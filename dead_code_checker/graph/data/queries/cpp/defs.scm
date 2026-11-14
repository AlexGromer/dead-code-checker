; graph.extract.ts_generic definitions of C++ (tree-sitter-cpp). Vocabulary as c/defs.scm.
; A name may be qualified (`Foo::bar`, `Foo::~Foo`): rules.json name_map turns "::" into "." so the out-of-class definition is Foo.bar.
; Namespaces are not definitions (their members are file-level names, a documented gap).

; free functions, methods defined in a class, constructors, destructors, out-of-class definitions
(function_definition declarator: (function_declarator declarator: [(identifier) (field_identifier) (destructor_name) (qualified_identifier)] @name)) @def.func
(function_definition declarator: (pointer_declarator declarator: (function_declarator declarator: [(identifier) (field_identifier) (qualified_identifier)] @name))) @def.func
(function_definition declarator: (reference_declarator (function_declarator declarator: [(identifier) (field_identifier) (qualified_identifier)] @name))) @def.func

; a method declared in a class (virtual / pure virtual interfaces have no body)
(field_declaration declarator: (function_declarator declarator: [(field_identifier) (destructor_name)] @name)) @def.func

; the exported surface of a header: a prototype at file level
(translation_unit (declaration declarator: (function_declarator declarator: [(identifier) (qualified_identifier)] @name)) @def.func @vis.exported)

(preproc_function_def name: (identifier) @name) @def.func

(class_specifier name: (type_identifier) @name body: (field_declaration_list)) @def.class
(struct_specifier name: (type_identifier) @name body: (field_declaration_list)) @def.class
(enum_specifier name: (type_identifier) @name body: (enumerator_list)) @def.class
(type_definition type: (struct_specifier !name body: (field_declaration_list)) declarator: (type_identifier) @name) @def.class
(alias_declaration name: (type_identifier) @name) @def.class

; modifiers: static (internal linkage, only on a function of a file or a namespace: `static` on a member of a class is a static member of
; its public API, so a function_definition inside field_declaration_list is not matched), virtual (dispatch), override / final
(translation_unit (function_definition (storage_class_specifier) @mod))
(declaration_list (function_definition (storage_class_specifier) @mod))
; an anonymous namespace is internal linkage too
(namespace_definition !name body: (declaration_list (function_definition) @vis.private))
(translation_unit (declaration (storage_class_specifier) @mod declarator: (function_declarator)))
(field_declaration "virtual" @mod)
(function_definition "virtual" @mod)
(function_definition (virtual_specifier) @mod)
(field_declaration (virtual_specifier) @mod)
