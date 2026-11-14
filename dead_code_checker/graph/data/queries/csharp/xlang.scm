; graph.extract.xlang (cross-language facts). Vocabulary: @call + @args a call and its argument list (the callee is the text between them),
; @lit a string, @ffi.decl + @name a method declaration that may bind a native symbol ([DllImport], [LibraryImport]). No predicates.
(invocation_expression arguments: (argument_list) @args) @call
(object_creation_expression arguments: (argument_list) @args) @call
(string_literal) @lit
(verbatim_string_literal) @lit
(method_declaration name: (identifier) @name) @ffi.decl
