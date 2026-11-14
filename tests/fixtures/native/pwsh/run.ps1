# deadcode: root script entry
. $PSScriptRoot\lib\common.ps1
. "$PSScriptRoot/lib/types.ps1"
Import-Module ./mod/Tools.psm1
. (Join-Path $PSScriptRoot 'lib/init.ps1')
function Main {
    param($n)
    Get-Used
    & "Dyn-$n"
    $f = ${function:Fvar-Target}
    [Helper]::Do()
    Invoke-Tool
    $w = New-Object Widget
    $c = Get-Command Plainfn
}
Main
