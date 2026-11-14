; graph.extract.xlang (cross-language facts). Vocabulary: @call + @args a call and its argument list (the callee is the text between them),
; @lit a string. No predicates: the mapping filters by text.
(call_expression arguments: (argument_list) @args) @call
(string_literal) @lit
