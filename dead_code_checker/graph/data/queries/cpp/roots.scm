; graph.extract.ts_generic roots of C++. Vocabulary as c/roots.scm.
(comment) @root.marker

(function_definition declarator: (function_declarator declarator: (identifier) @root.main))

(function_definition (attribute_specifier) @root.annotation.c-attribute)
(function_definition (ms_declspec_modifier) @root.annotation.c-attribute)

; extern "C" { int f() {...} }  and  extern "C" int f() {...}: C linkage means an outside caller (dlsym, a C program)
(linkage_specification body: (function_definition) @root.annotation.extern-c)
(linkage_specification body: (declaration_list (function_definition) @root.annotation.extern-c))

; googletest / Catch2 / Boost.Test macros look like a function definition: TEST(Suite, Name) { ... }
(function_definition declarator: (function_declarator declarator: (identifier) @root.test))

; swap, begin, end, to_json, from_json ...: a free function that the language or a library finds by argument-dependent lookup, nobody calls it by name
(function_definition declarator: (function_declarator declarator: (identifier) @root.annotation.adl))

; a member that a framework or a registry calls by convention, nothing in the tree names it: the singleton factory (`static Foo &instance()`), the route registration
; and the entry of a command (rules.json roots annotation.cpp-hook): out of class (`Foo::instance`), declared in the class, defined in the class
(function_declarator declarator: (qualified_identifier name: (identifier) @root.annotation.cpp-hook))
(function_declarator declarator: (field_identifier) @root.annotation.cpp-hook)
