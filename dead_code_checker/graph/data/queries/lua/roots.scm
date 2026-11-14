; graph.extract.ts_generic roots of Lua. @root.marker = a comment (`-- deadcode: root <why>` roots the definition on or below that line);
; @root.main = a function named main (rules.json roots filters the text).
(comment) @root.marker
(function_declaration name: (identifier) @root.main)
