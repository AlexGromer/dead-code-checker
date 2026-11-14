; graph.extract.xlang (cross-language facts). Vocabulary: @call + @args a call and its argument list (the callee is the text between them),
; @lit a string, @ffi.decl + @name a function declared in an `extern "C" { .. }` block (a native symbol the crate imports). No predicates.
(call_expression arguments: (arguments) @args) @call
(string_literal) @lit
(foreign_mod_item (declaration_list (function_signature_item name: (identifier) @name) @ffi.decl))
