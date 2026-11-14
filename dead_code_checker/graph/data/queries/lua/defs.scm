; graph.extract.ts_generic definitions of Lua. @def.<kind> = the whole definition, @name = its name (a dotted `M.f` or `M:f` stays as written,
; rules.json name_map spells the colon as a dot). No predicates: the mapping filters by text.

; function f() end, local function f() end
(function_declaration name: (identifier) @name) @def.func
; function M.f() end, function M.sub.f() end
(function_declaration name: (dot_index_expression) @name) @def.func
; function M:f() end
(function_declaration name: (method_index_expression) @name) @def.func
; local f = function() end, f = function() end (the first name and the first value)
(assignment_statement (variable_list . name: (identifier) @name) (expression_list . value: (function_definition))) @def.func
; M.f = function() end
(assignment_statement (variable_list . name: (dot_index_expression) @name) (expression_list . value: (function_definition))) @def.func
; local M = {}, M = {}: a table that holds functions is the module or class of `function M.f()` (the functions are its members)
(assignment_statement (variable_list . name: (identifier) @name) (expression_list . value: (table_constructor))) @def.class
