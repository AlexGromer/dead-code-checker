; graph.extract.ts_generic stub detection. Capture vocabulary: @stub.def = the whole definition, @body = its brace block;
; the mapping hands the text between the braces to core.generic_body_stub.
(function_definition name: (word) @name body: (compound_statement) @body) @stub.def
