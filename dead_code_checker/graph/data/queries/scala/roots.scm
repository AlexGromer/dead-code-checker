; graph.extract.ts_generic roots of Scala. @root.marker = a comment (`// deadcode: root <why>`); @root.main = a method named main or an
; object that extends App (rules.json roots filters the text).
(comment) @root.marker
(function_definition name: (identifier) @root.main)
(object_definition extend: (extends_clause (type_identifier) @root.main-app))
