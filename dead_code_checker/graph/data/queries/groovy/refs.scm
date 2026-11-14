; graph.extract.ts_generic uses of Groovy. The grammar has no expression structure: a call is a `func` (name and argument block) and every other
; word is an identifier. A word that names a definition is a weak use of it (tier N), so a definition no word of the project mentions is dead.

(func . (identifier) @name) @ref.call
(func . (identifier) @name @member) @ref.call
(identifier) @name @ref.ref
; load 'x.groovy', evaluate: a script of ours
(command (unit (identifier) @cmd) . (string) @name) @ref.include
