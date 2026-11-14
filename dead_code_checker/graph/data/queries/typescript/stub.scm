; graph.extract.ts_generic stub detection of TypeScript: the text between the braces goes to core.generic_body_stub("js").
(function_declaration name: (identifier) @name body: (statement_block) @body) @stub.def
(method_definition name: (_) @name body: (statement_block) @body) @stub.def
(variable_declarator name: (identifier) @name value: (arrow_function body: (statement_block) @body)) @stub.def
