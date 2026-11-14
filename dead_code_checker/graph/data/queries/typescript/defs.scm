; graph.extract.ts_generic definitions of TypeScript. @def.<kind> = the whole definition, @name = its identifier. No predicates.
; A function expression (`const f = function () {}`) is not matched: its node name differs between grammar versions.

; function f() {}, function* g() {}, class C {}, class C { m() {} }
(function_declaration name: (identifier) @name) @def.func
(generator_function_declaration name: (identifier) @name) @def.func
(class_declaration name: (_) @name) @def.class
(abstract_class_declaration name: (_) @name) @def.class
; a decorated class or member (Angular, NestJS, Lit, MobX): @deco = the decorator; the framework that reads it registers the definition
(class_declaration decorator: (decorator) @deco name: (_) @name) @def.class
(abstract_class_declaration decorator: (decorator) @deco name: (_) @name) @def.class
(export_statement decorator: (decorator) @deco declaration: (class_declaration name: (_) @name) @def.class)
; the decorators of a member are siblings of it in the class body (not a field of the method, as they are in the JavaScript grammar):
; `+` gives all of them to one match, so every registering decorator of `@Get() @memoize handler()` is seen
(class_body (decorator)+ @deco . (method_definition name: (_) @name) @def.method)
(interface_declaration name: (_) @name) @def.iface
(enum_declaration name: (_) @name) @def.class
; only in a class body: a method of an object literal (`{ onClick() {} }`) is no symbol
(class_body (method_definition name: (_) @name) @def.method)

; const f = () => {}
(variable_declarator name: (identifier) @name value: (arrow_function)) @def.func
