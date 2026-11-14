; graph.extract.ts_generic uses of Scala. @ref.<kind> = the use, @name = the name, @member = also a member of that name, @scope = the derived
; type of an inherit, @dyn = a dynamic site. Imports are no uses: the type or value that is written out is.

; f(1), recv.f(1), new T(1)
(call_expression function: (identifier) @name) @ref.call
(call_expression function: (field_expression field: (identifier) @name @member)) @ref.call
(call_expression function: (generic_function function: (identifier) @name)) @ref.call
(instance_expression (type_identifier) @name) @ref.call
; Obj.member, a.b: the object and the member are uses too
(field_expression value: (identifier) @name) @ref.ref
(field_expression field: (identifier) @name @member) @ref.ref
; type annotations, extends, type arguments
(type_identifier) @name @ref.ref
; class A extends B with C
(class_definition name: (identifier) @scope extend: (extends_clause (type_identifier) @name)) @ref.inherit
(object_definition name: (identifier) @scope extend: (extends_clause (type_identifier) @name)) @ref.inherit
(trait_definition name: (identifier) @scope extend: (extends_clause (type_identifier) @name)) @ref.inherit
(class_definition name: (identifier) @scope extend: (extends_clause (generic_type (type_identifier) @name))) @ref.inherit
; a method or value passed on: f(handler), a + b, val x = y, { y }
(arguments (identifier) @name) @ref.ref
(infix_expression (identifier) @name) @ref.ref
(val_definition value: (identifier) @name) @ref.ref
(var_definition value: (identifier) @name) @ref.ref
(block (identifier) @name) @ref.ref
; Class.forName("a.B"), getMethod("m"): a name given as a string (tier D)
(call_expression function: (field_expression field: (identifier) @cmd) arguments: (arguments . (string) @dyn)) @block
; a bare name is a call on this (an inherited member or one of a trait: rules self_word) unless a parameter, a lambda or a pattern binds it (@local)
(identifier) @name @self @ref.ref
(parameter name: (identifier) @local)
(class_parameter name: (identifier) @local)
(lambda_expression parameters: (identifier) @local)
(binding name: (identifier) @local)
(case_clause pattern: (identifier) @local)
(tuple_pattern (identifier) @local)
(case_class_pattern pattern: (identifier) @local)
(typed_pattern pattern: (identifier) @local)
(infix_pattern (identifier) @local)
(capture_pattern name: (identifier) @local)
; import app.Imp._, import app.Imp.given, import app.Imp.{a, b => c}, import app.Imp.a: the implicit members of Imp are in scope of this file and used
; without being named (rules.json dyn.implicit: the pattern implicit:Imp, link.py); the type that holds them is the identifier before the last part
(import_declaration (identifier) @dyn.implicit . (namespace_wildcard))
(import_declaration (identifier) @dyn.implicit . (namespace_selectors))
(import_declaration (identifier) @dyn.implicit . (identifier) .)
