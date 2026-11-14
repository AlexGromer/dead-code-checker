; graph.extract.ts_generic uses of Starlark. @ref.<kind> = the use, @name = the name, @member = also a member of that name, @cmd = the callee of a
; load pattern.

; f(), native.f(), obj.f()
(call function: (identifier) @name) @ref.call
(call function: (attribute attribute: (identifier) @name @member)) @ref.call
; implementation = _impl, f(handler), [a, b], {"k": v}, x = y, return f
(argument_list (identifier) @name) @ref.ref
(keyword_argument value: (identifier) @name) @ref.ref
(list (identifier) @name) @ref.ref
(dictionary (pair value: (identifier) @name)) @ref.ref
(assignment right: (identifier) @name) @ref.ref
(return_statement (identifier) @name) @ref.ref
; load("//pkg:f.bzl", "sym", alias = "other"): the file is ours (rules.json path_sub), the symbols are uses of its definitions
(call function: (identifier) @cmd arguments: (argument_list . (string (string_content) @name))) @ref.include
(call function: (identifier) @cmd arguments: (argument_list (string (string_content) @name))) @ref.ref
(call function: (identifier) @cmd arguments: (argument_list (keyword_argument value: (string (string_content) @name)))) @ref.ref
