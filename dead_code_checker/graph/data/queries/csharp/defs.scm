; graph.extract.ts_generic definitions of C# (tree-sitter-c-sharp). Vocabulary as c/defs.scm.
; Namespaces are not definitions (a type is named without its namespace: a documented gap).

(class_declaration name: (identifier) @name) @def.class
(struct_declaration name: (identifier) @name) @def.class
(record_declaration name: (identifier) @name) @def.class
(enum_declaration name: (identifier) @name) @def.class
(interface_declaration name: (identifier) @name) @def.iface

; a func inside a class / interface becomes a method (the mapping), a property or a field is a var
(method_declaration name: (identifier) @name) @def.func
(constructor_declaration name: (identifier) @name) @def.func
(local_function_statement name: (identifier) @name) @def.func
(property_declaration name: (identifier) @name) @def.var
(field_declaration (variable_declaration (variable_declarator (identifier) @name))) @def.var

; public, private, internal, protected, static, abstract, virtual, override (direct children of the declaration)
(class_declaration (modifier) @mod)
(struct_declaration (modifier) @mod)
(record_declaration (modifier) @mod)
(enum_declaration (modifier) @mod)
(interface_declaration (modifier) @mod)
(method_declaration (modifier) @mod)
(constructor_declaration (modifier) @mod)
(property_declaration (modifier) @mod)
(field_declaration (modifier) @mod)
