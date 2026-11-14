; graph.extract.ts_generic definitions of JavaScript. @def.<kind> = the whole definition, @name = its identifier. No predicates.
; A function expression (`const f = function () {}`) is not matched: its node name differs between grammar versions.

; function f() {}, function* g() {}, class C {}, class C { m() {} }
(function_declaration name: (identifier) @name) @def.func
(generator_function_declaration name: (identifier) @name) @def.func
(class_declaration name: (identifier) @name) @def.class
; only in a class body: a method of an object literal (`{ onClick() {} }`) is no symbol
(class_body (method_definition name: (_) @name) @def.method)

; a decorated class or member (Angular, NestJS, Lit, MobX): @deco = the decorator; the framework that reads it registers the definition
(class_declaration decorator: (decorator) @deco name: (identifier) @name) @def.class
(export_statement decorator: (decorator) @deco declaration: (class_declaration name: (identifier) @name) @def.class)
(class_body (method_definition decorator: (decorator) @deco name: (_) @name) @def.method)

; const f = () => {}
(variable_declarator name: (identifier) @name value: (arrow_function)) @def.func
