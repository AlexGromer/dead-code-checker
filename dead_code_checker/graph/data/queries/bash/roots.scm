; graph.extract.ts_generic roots. Capture vocabulary: @root.marker = a comment; the mapping keeps `# deadcode: root <why>` and
; makes the definition on or just below that line a manual root (module root when there is none).
(comment) @root.marker

; a command that runs at the top level of the file (not inside a function): the file is a script, running it runs the command (rules.json roots main-toplevel)
(command name: (command_name) @root.main-toplevel)
