; graph.extract.ts_generic definitions of Kotlin. Capture vocabulary: see java/defs.scm. Node names are from the
; fwcd/tree-sitter-kotlin grammar. A top-level fun is a func, a fun in a class a method (the mapping
; turns it), an `interface` is a class_declaration with the interface keyword.
(class_declaration (modifiers)? @mods "interface" (type_identifier) @name) @def.iface
(class_declaration (modifiers)? @mods "class" (type_identifier) @name) @def.class
(object_declaration (modifiers)? @mods (type_identifier) @name) @def.class
(function_declaration (modifiers)? @mods (simple_identifier) @name) @def.func
; a typealias is a def like a class: a use of the alias is a use of the type it stands for (the ref inside its span), whatever file holds it
(type_alias (modifiers)? @mods (type_identifier) @name) @def.class
; a property is a def at the top level of a file only (the mapping drops the others): its initializer runs with the file, and another file
; uses it by name. Same shape as the @var.field pattern of types.scm.
(property_declaration (modifiers)? @mods (variable_declaration (simple_identifier) @name)) @def.var
; @scope: see java/defs.scm
(secondary_constructor) @scope
(anonymous_initializer) @scope
(lambda_literal) @scope
; @anon: see java/defs.scm. `object : T { ... }` in a property, an argument or a function
(object_literal) @anon
(enum_entry (class_body) @anon)
