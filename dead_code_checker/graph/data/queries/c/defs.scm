; graph.extract.ts_generic definitions of C (tree-sitter-c). Vocabulary: @def.<kind> the whole definition, @name its identifier, @mod a
; modifier (rules.json vis_mods: `static` = private), @vis.<vis> an explicit visibility.

; int f(void) { ... }, int *f(void) { ... }
(function_definition declarator: (function_declarator declarator: (identifier) @name)) @def.func
(function_definition declarator: (pointer_declarator declarator: (function_declarator declarator: (identifier) @name))) @def.func

; the exported surface of a header: a prototype at file level (a `static` prototype is made private by its @mod)
(translation_unit (declaration declarator: (function_declarator declarator: (identifier) @name)) @def.func @vis.exported)
(translation_unit (declaration declarator: (pointer_declarator declarator: (function_declarator declarator: (identifier) @name))) @def.func @vis.exported)
; the same inside an include guard or a conditional (`#ifndef X_H ... #endif`): the real grammar nests the declarations in preproc_ifdef / preproc_if
(translation_unit (preproc_ifdef (declaration declarator: (function_declarator declarator: (identifier) @name)) @def.func @vis.exported))
(translation_unit (preproc_ifdef (declaration declarator: (pointer_declarator declarator: (function_declarator declarator: (identifier) @name))) @def.func @vis.exported))
(translation_unit (preproc_if (declaration declarator: (function_declarator declarator: (identifier) @name)) @def.func @vis.exported))
(translation_unit (preproc_if (declaration declarator: (pointer_declarator declarator: (function_declarator declarator: (identifier) @name))) @def.func @vis.exported))

; #define F(x) ... (an object-like macro is mostly an include guard or a flag: not a definition)
(preproc_function_def name: (identifier) @name) @def.func

; struct S { ... }, enum E { ... }; typedef struct { ... } T; (a tagged typedef `typedef struct S {..} S;` is the struct S: one def)
(struct_specifier name: (type_identifier) @name body: (field_declaration_list)) @def.class
(enum_specifier name: (type_identifier) @name body: (enumerator_list)) @def.class
(type_definition type: (struct_specifier !name body: (field_declaration_list)) declarator: (type_identifier) @name) @def.class
(type_definition type: (enum_specifier !name body: (enumerator_list)) declarator: (type_identifier) @name) @def.class

; typedef struct tag { ... } tag_t;  tag_t is the tag under another name: the code names the typedef, never the tag (@alias uses @aliased, ts_generic)
(type_definition type: (struct_specifier name: (type_identifier) @aliased body: (field_declaration_list)) declarator: (type_identifier) @alias)
(type_definition type: (enum_specifier name: (type_identifier) @aliased body: (enumerator_list)) declarator: (type_identifier) @alias)

; static, inline ... of a function (direct children only: a `static` local variable is not the function's)
(function_definition (storage_class_specifier) @mod)
(translation_unit (declaration (storage_class_specifier) @mod declarator: (function_declarator)))
(translation_unit (preproc_ifdef (declaration (storage_class_specifier) @mod declarator: (function_declarator))))
(translation_unit (preproc_if (declaration (storage_class_specifier) @mod declarator: (function_declarator))))
