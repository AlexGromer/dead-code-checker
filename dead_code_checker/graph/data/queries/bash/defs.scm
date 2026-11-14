; graph.extract.ts_generic definitions. Capture vocabulary: @def.<kind> = the whole definition (kind: a model.NODE_KINDS
; name), @name = its identifier. No predicates: the mapping filters by text (predicate support is binding-version dependent).

; foo() { ... }, function foo { ... }, function foo() { ... }
(function_definition name: (word) @name) @def.func
