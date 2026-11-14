; graph.extract.ts_generic roots for Go. Vocabulary: @root.marker a comment (`// deadcode: root`, `//export Name`), @root.cand a
; top-level function with its @name (data/rules.json roots: main in package main, init), @pkg the declared package name.
(comment) @root.marker
(package_clause (package_identifier) @pkg)
(source_file (function_declaration name: (identifier) @name) @root.cand)
