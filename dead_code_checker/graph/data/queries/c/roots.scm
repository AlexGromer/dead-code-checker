; graph.extract.ts_generic roots of C. Vocabulary: @root.marker a comment (`deadcode: root <why>`); @root.<suffix> a node whose text is
; filtered by rules.json "roots" and whose containing definition becomes the root.
(comment) @root.marker

; int main(...)  (also wmain, WinMain, DllMain: rules.json)
(function_definition declarator: (function_declarator declarator: (identifier) @root.main))

; __attribute__((constructor | destructor | used | visibility("default"))), __declspec(dllexport)
(function_definition (attribute_specifier) @root.annotation.c-attribute)
(function_definition (ms_declspec_modifier) @root.annotation.c-attribute)

; a test function of a test framework that registers by naming: cmocka/unity `void test_x(void)` is referenced from its table (refs.scm)
