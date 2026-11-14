; graph.extract.ts_generic definitions of Java. Capture vocabulary: @scope = a region that is no definition (see below), @def.<kind> = the whole definition (kind: a model.NODE_KINDS
; name), @name = its identifier, @mods = its modifiers node (annotations included: lang_rules reads visibility, abstract,
; @Override and the framework annotations from its text). A constructor is no definition: its body belongs to its class.
; @scope = a region that is no definition (constructor, lambda, initializer): its parameters and locals are its own, the code in it
; belongs to the enclosing definition. A method inside a method (anonymous or local class) is a definition of its own.
; Fields are no definitions (unused private fields are PMD's). Node names are from
; tree-sitter-java.
(class_declaration (modifiers)? @mods name: (identifier) @name) @def.class
(record_declaration (modifiers)? @mods name: (identifier) @name) @def.class
(enum_declaration (modifiers)? @mods name: (identifier) @name) @def.class
(interface_declaration (modifiers)? @mods name: (identifier) @name) @def.iface
(method_declaration (modifiers)? @mods name: (identifier) @name) @def.method
; @meta = the definition is an annotation type (an @interface): the rules of its own annotations are handed on to the definitions
; that use it (lang_rules: composed annotations). An annotation type is an interface.
(annotation_type_declaration (modifiers)? @mods name: (identifier) @name) @def.iface @meta
(constructor_declaration) @scope
(compact_constructor_declaration) @scope
(lambda_expression) @scope
(static_initializer) @scope
(class_body (block) @scope)
; @anon = the body of an anonymous class: its methods are defs that nothing names, they live with the method or the class that holds
; the body (`new T() {...}` in a field or a method, the body of an enum constant)
(object_creation_expression (class_body) @anon)
(enum_constant (class_body) @anon)
