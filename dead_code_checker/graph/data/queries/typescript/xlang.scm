; graph.extract.xlang (cross-language facts). Vocabulary: @call + @args a call and its argument list (the callee is the text between them),
; @lit a string, @env.r / @env.w with @obj and @name a member or subscript of an environment object (process.env.X, process.env["X"]).
; No predicates: the mapping filters by text.
(call_expression arguments: (arguments) @args) @call
(new_expression arguments: (arguments) @args) @call
(string) @lit
(template_string) @lit
(member_expression object: (_) @obj property: (property_identifier) @name) @env.r
(subscript_expression object: (_) @obj index: (string) @name) @env.r
(assignment_expression left: (member_expression object: (_) @obj property: (property_identifier) @name) @env.w)
(assignment_expression left: (subscript_expression object: (_) @obj index: (string) @name) @env.w)
