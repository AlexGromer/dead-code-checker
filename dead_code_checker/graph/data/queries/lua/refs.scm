; graph.extract.ts_generic uses of Lua. @ref.<kind> = the use, @name = the name, @cmd = the callee of an include pattern, @dyn = a dynamic site.

; f(), M.f(), obj:m(): the linker decides what the name is (rules.json name_map spells `obj:m` as `obj.m`)
(function_call name: (identifier) @name) @ref.call
(function_call name: (dot_index_expression) @name) @ref.call
(function_call name: (method_index_expression) @name) @ref.call

; require("a.b"), require "a.b", dofile("x.lua"), loadfile("x.lua"): the first argument is a module or file of ours (rules.json ref_cmd, path_sub)
(function_call name: (identifier) @cmd arguments: (arguments . (string content: (string_content) @name))) @ref.include
; pcall(require, "a.b"), pcall(dofile, "x.lua"): the optional module; the first argument is the callee
(function_call arguments: (arguments . (identifier) @cmd . (string content: (string_content) @name))) @ref.include

; a function passed or stored as a value, bare or a member of a module (register(u.g), {on = u.h}, u.k = u.m): handlers, callbacks, table fields (tier N, by name)
(arguments [(identifier) (dot_index_expression)] @name) @ref.ref
(field value: [(identifier) (dot_index_expression)] @name) @ref.ref
(assignment_statement (expression_list value: [(identifier) (dot_index_expression)] @name)) @ref.ref
(return_statement (expression_list [(identifier) (dot_index_expression)] @name)) @ref.ref

; _G[name](), t[name](): the callee is only known at run time (tier D)
(function_call name: (bracket_index_expression) @dyn)
; require(name), dofile(path .. ".lua"): a computed module (rules.json ref_cmd.dyn keeps the include calls)
(function_call name: (identifier) @cmd arguments: (arguments . [(identifier) (binary_expression) (function_call)] @dyn)) @block
