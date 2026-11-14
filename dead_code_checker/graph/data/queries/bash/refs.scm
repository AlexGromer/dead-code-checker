; graph.extract.ts_generic uses. Capture vocabulary: @ref.<kind> = the use (kind: call | ref | include ...), @name = the name,
; @cmd = the command word of an include pattern, @dyn = a dynamic site. No predicates: the mapping filters by text.

; foo args: a command word is a call of the function foo (or of an external command: the linker decides)
(command name: (command_name (word) @name)) @ref.call

; source lib.sh, . lib.sh: the first argument of any command; the mapping keeps it only for the include commands
(command name: (command_name (word) @cmd) . argument: (_) @name) @ref.include

; trap cleanup EXIT, xargs -n1 handler: a plain word argument may name a function (tier N, by name)
(command argument: (word) @name) @ref.ref

; $cmd args, ${cmd} args: the command is only known at run time (tier D)
(command name: (command_name [(simple_expansion) (expansion)] @dyn))

; "check_$t" args, check_${t} args, "run_${a}_x": a command word that starts with literal text and goes on with an expansion names a function by its
; prefix (tier D, the glob name:check_*; rules.json dyn.name keeps only a prefix that can start a function name, a path `"$D/tool"` or `/x/$y` is none)
(command name: (command_name (string . (string_content) @dyn.name . [(simple_expansion) (expansion)])))
(command name: (command_name (concatenation . (word) @dyn.name . [(simple_expansion) (expansion)])))

; export -f f g, declare -f f: the names are functions (a child shell or xargs calls them by name); `export VAR` is a use of a function VAR at worst (N, by name)
(declaration_command (variable_name) @name) @ref.ref

; bash -c 'f "$1"', xargs ... sh -c "f {}": the string is script text (rules.json scan_before keeps it after -c), its words are calls and uses
(command argument: (raw_string) @scan)
(command argument: (string) @scan)
