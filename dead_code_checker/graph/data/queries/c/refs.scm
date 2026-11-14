; graph.extract.ts_generic uses of C. Vocabulary: @ref.<kind> the use, @name the name. No predicates.

; f(x)  p->f(x)  s.f(x): the mapping turns "p->f" into "p.f" (rules.json name_map) and a computed receiver into "?.f"
(call_expression function: (identifier) @name) @ref.call
(call_expression function: (field_expression) @name) @ref.call

; #include "a/b.h" (a <system> header is not a file of the project)
(preproc_include path: (string_literal) @name) @ref.include

; Every identifier is a weak use, whatever its position: a function name as a value in a callback table, an assignment, an argument, `&f`, a
; return, a comparison, a case label, and inside a cast `(void*)handler`, parentheses, a unary or a subscript expression, a `sizeof` or a
; compound literal. No list of positions can miss one. The identifier that DECLARES a definition (or sits inside its name) is skipped by
; ts_generic (no self edge). Cost: a local or a parameter that shares its name with a function counts as a use of it (over-alive, never a false dead).
(identifier) @name @ref.ref

; a struct / enum / typedef name used as a type
(type_identifier) @name @ref.ref

; #define F(x) bar(x) / #define H handler: the body is one opaque token (preproc_arg); ts_generic reads its identifiers (@scan), a `##` paste is dyn
(preproc_function_def (preproc_arg) @scan)
(preproc_def (preproc_arg) @scan)
