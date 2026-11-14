# graph.extract.pwsh worker: one JSON request per stdin line {"text": "<source>"}, one JSON reply per stdout line.
# errors counts syntax errors only (a module or type that does not resolve here is not one).
# The source is only PARSED ([System.Management.Automation.Language.Parser]::ParseInput), never run. The reply is the flat list of
# the AST nodes the extractor needs: {"errors": n, "rec": [[kind, name, startLine, startCol, endLine, endCol, extra...], ...]}
# Record kinds: func class method prop cmd member type dyn fvar using hash str (layout: graph/extract/pwsh.py _facts; each is
# [kind, name, startLine, startCol, endLine, endCol, extra...]).
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Management.Automation

function Pos($n) { $e = $n.Extent; return @($e.StartLineNumber, $e.StartColumnNumber, $e.EndLineNumber, $e.EndColumnNumber) }
function Str($n) {
    if ($n -is [System.Management.Automation.Language.StringConstantExpressionAst]) { return $n.Value }
    if ($n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { return $n.Value }
    return $null
}
function Strings($n) {
    $out = @()
    if ($null -eq $n) { return $out }
    foreach ($s in $n.FindAll({ param($x) $x -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) { $out += $s.Value }
    return , $out
}

# literals of a computed command word, in source order (a bareword is a command or argument name of the expression, not a literal)
function Frags($n) {
    $out = @()
    foreach ($s in $n.FindAll({ param($x) ($x -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $x.StringConstantType -ne 'BareWord') -or $x -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true)) { $out += $s.Value }
    return , $out
}
# the arguments of a command or a call: [constant strings and the strings nested in an expression (a parameter is '-Name'), the text of every computed element]
function ArgVals($els) {
    $strs = @(); $computed = @()
    foreach ($el in $els) {
        if ($el -is [System.Management.Automation.Language.CommandParameterAst]) { $strs += ('-' + $el.ParameterName); continue }
        $s = Str $el
        if ($null -ne $s) { $strs += $s; continue }
        if ($el -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -or $el -is [System.Management.Automation.Language.ConstantExpressionAst]) { continue }   # a block is parsed in place, a number is data
        $strs += (Strings $el); $computed += $el.Extent.Text
    }
    return , @($strs, $computed)
}
# an expandable string that holds anything but the script's own location is computed
function Dynamic($n) {
    return @($n.NestedExpressions | Where-Object { $_ -isnot [System.Management.Automation.Language.VariableExpressionAst] -or $_.VariablePath.UserPath -notin @('PSScriptRoot', 'PSCommandPath') }).Count -gt 0
}

$Named = '\.(ps1|psm1|psd1)$|^[A-Za-z]+-[A-Za-z][\w-]*$'

function Records($ast) {
    $rec = [System.Collections.Generic.List[object]]::new()
    foreach ($n in $ast.FindAll({ $true }, $true)) {
        if ($n.Extent.StartLineNumber -le 0) { continue }   # synthetic nodes (an implicit constructor body)
        $p = Pos $n
        switch ($n.GetType().Name) {
            'FunctionDefinitionAst' {
                if ($n.Parent -is [System.Management.Automation.Language.FunctionMemberAst]) { break }
                $cnt = if ($n.Body.EndBlock) { $n.Body.EndBlock.Statements.Count } else { 0 }
                $alias = [bool]($n.Body.ParamBlock -and @($n.Body.ParamBlock.Attributes | Where-Object { $_.TypeName.Name -eq 'Alias' }).Count)   # [Alias('x')] on the function
                $rec.Add(@('func', $n.Name) + $p + @([bool]$n.IsFilter, $cnt, $alias))
            }
            'TypeDefinitionAst' {
                $k = if ($n.IsEnum) { 'enum' } elseif ($n.IsInterface) { 'interface' } else { 'class' }
                $b = @($n.BaseTypes | ForEach-Object { $_.TypeName.Name })
                $rec.Add(@('class', $n.Name) + $p + @(, $b) + @($k))
            }
            'FunctionMemberAst' {
                $cnt = if ($n.Body.EndBlock) { $n.Body.EndBlock.Statements.Count } else { 0 }
                $rec.Add(@('method', $n.Name) + $p + @([bool]$n.IsConstructor, $cnt, [bool]$n.IsStatic))
            }
            'PropertyMemberAst' { $rec.Add(@('prop', $n.Name) + $p) }
            'CommandAst' {
                $first = $n.CommandElements[0]
                $form = switch ($n.InvocationOperator.ToString()) { 'Dot' { 'dot' } 'Ampersand' { 'amp' } default { 'call' } }
                $av = ArgVals ($n.CommandElements | Select-Object -Skip 1)
                # the command word is known only when it is a constant (`. $PSScriptRoot\x.ps1` too); `& $x`, `& "Get-$n"`, `. (Join-Path ..)`
                # are computed: a dyn site [text, form, shape var|str|expr, literal pieces of the expression]
                $name = $null
                if ($first -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $name = $first.Value }
                elseif ($first -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and -not (Dynamic $first)) { $name = $first.Value }
                if ($null -eq $name) {
                    $shape = if ($first -is [System.Management.Automation.Language.VariableExpressionAst]) { 'var' } elseif ($first -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { 'str' } else { 'expr' }
                    $rec.Add(@('dyn', $first.Extent.Text) + $p + @($form, $shape, @(, @(Frags $first))))
                    continue
                }
                $rec.Add(@('cmd', $name) + $p + @($form, @(, $av[0]), @(, $av[1])))
            }
            'VariableExpressionAst' {
                if ($n.VariablePath.UserPath -match '^function:(.+)$') { $rec.Add(@('fvar', $Matches[1]) + $p) }   # ${function:Name}, $function:Name
            }
            'InvokeMemberExpressionAst' {
                $m = Str $n.Member
                $isS = [bool]$n.Static
                $recv = if ($n.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) { $n.Expression.TypeName.Name } elseif ($n.Expression -is [System.Management.Automation.Language.VariableExpressionAst]) { '$' + $n.Expression.VariablePath.UserPath } else { '' }
                $av = ArgVals $n.Arguments
                if ($null -eq $m) { $rec.Add(@('dyn', $n.Extent.Text) + $p) } else { $rec.Add(@('member', $m) + $p + @($isS, $recv, $true, @(, $av[0]), @(, $av[1]))) }
            }
            'MemberExpressionAst' {
                $m = Str $n.Member
                $isS = [bool]$n.Static
                $recv = if ($n.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) { $n.Expression.TypeName.Name } elseif ($n.Expression -is [System.Management.Automation.Language.VariableExpressionAst]) { '$' + $n.Expression.VariablePath.UserPath } else { '' }
                if ($null -eq $m) { $rec.Add(@('dyn', $n.Extent.Text) + $p) } else { $rec.Add(@('member', $m) + $p + @($isS, $recv, $false)) }
            }
            'StringConstantExpressionAst' { if ($n.Value -match $Named) { $rec.Add(@('str', $n.Value) + $p) } }   # a string that names a script or a Verb-Noun command
            'ExpandableStringExpressionAst' { if ($n.Value -match $Named) { $rec.Add(@('str', $n.Value) + $p) } }
            'TypeConstraintAst' { if ($n.Parent -isnot [System.Management.Automation.Language.TypeDefinitionAst]) { $rec.Add(@('type', $n.TypeName.Name) + $p) } }
            'TypeExpressionAst' { $rec.Add(@('type', $n.TypeName.Name) + $p) }
            'UsingStatementAst' {
                $nm = if ($n.Name) { $n.Name.Value } elseif ($n.ModuleSpecification) { (Strings $n.ModuleSpecification) -join ',' } else { '' }
                $rec.Add(@('using', $nm) + $p + @($n.UsingStatementKind.ToString()))
            }
            'HashtableAst' {
                foreach ($kv in $n.KeyValuePairs) {
                    $k = Str $kv.Item1
                    if ($null -ne $k) { $rec.Add(@('hash', $k) + $p + @(, @(Strings $kv.Item2))) }
                }
            }
        }
    }
    return $rec
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    try {
        $req = $line | ConvertFrom-Json
        $tok = $null; $err = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseInput([string]$req.text, [ref]$tok, [ref]$err)
        $out = [ordered]@{ errors = @($err | Where-Object { $_.ErrorId -notin @('ModuleNotFoundDuringParse', 'TypeNotFound') }).Count; rec = @(Records $ast) }
    }
    catch { $out = [ordered]@{ errors = 1; rec = @(); fail = $_.Exception.Message } }
    [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress -Depth 8))
    [Console]::Out.Flush()
}
