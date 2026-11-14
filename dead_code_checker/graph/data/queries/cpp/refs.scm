; graph.extract.ts_generic uses of C++. Vocabulary as c/refs.scm.
(call_expression function: (identifier) @name) @ref.call
(call_expression function: (field_expression) @name) @ref.call
(call_expression function: (qualified_identifier) @name) @ref.call
(call_expression function: (template_function name: (identifier) @name)) @ref.call
(new_expression type: (type_identifier) @name) @ref.call

(preproc_include path: (string_literal) @name) @ref.include

; class D : public B, B2 { ... }: the scope of the match is the derived class (the clause lies inside its span)
(base_class_clause (type_identifier) @name) @ref.inherit
(base_class_clause (qualified_identifier) @name) @ref.inherit


; Every identifier is a weak use, whatever its position (a value in an initializer, an argument, a cast `(void*)handler`, a ternary, a
; subscript, `&Foo::bar`, a lambda capture ...), as in c/refs.scm; a name that DECLARES a definition (or sits inside its name) is not a use.
(identifier) @name @ref.ref
(type_identifier) @name @ref.ref
(qualified_identifier) @name @ref.ref

; #define F(x) bar(x) / #define H handler: the body is one opaque token (preproc_arg); ts_generic reads its identifiers (@scan), a `##` paste is dyn
(preproc_function_def (preproc_arg) @scan)
(preproc_def (preproc_arg) @scan)
