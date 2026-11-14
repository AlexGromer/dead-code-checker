; graph.extract.ts_generic definitions of Nix. @def.<kind> = the whole binding, @name = its (single) attribute name, @vis.<vis> = an explicit visibility.
; A let binding is private to its let; a function bound in an attribute set is the API of the file (exported). Other attribute bindings
; (options, packages, `services.foo.enable = true`) are data of the configuration, not definitions.

(let_expression (binding_set (binding (attrpath . (identifier) @name .) (function_expression)) @def.func @vis.private))
(let_expression (binding_set (binding (attrpath . (identifier) @name .)) @def.var @vis.private))
(attrset_expression (binding_set (binding (attrpath . (identifier) @name .) (function_expression)) @def.func @vis.exported))
(rec_attrset_expression (binding_set (binding (attrpath . (identifier) @name .) (function_expression)) @def.func @vis.exported))
