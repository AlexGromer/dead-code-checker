; graph.extract.ts_generic definitions of Starlark (Bazel .bzl / BUILD, Tilt). @def.<kind> = the whole definition, @name = its name. A name that starts with
; an underscore is private to its file (the default rule of ts_generic).

(function_definition name: (identifier) @name) @def.func
; MY_RULE = rule(...), CONSTANT = [...]: a top-level binding
(module (expression_statement (assignment left: (identifier) @name) @def.var))
