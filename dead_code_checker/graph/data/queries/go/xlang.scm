; graph.extract.xlang (cross-language facts). Vocabulary: @call + @args a call and its argument list (the callee is the text between them),
; @lit a string, @ffi.use + @obj + @name cgo `C.fn` (kept only in a file with import "C"). No predicates: the mapping filters by text.
(call_expression arguments: (argument_list) @args) @call
(interpreted_string_literal) @lit
(raw_string_literal) @lit
(selector_expression operand: (identifier) @obj field: (field_identifier) @name) @ffi.use
