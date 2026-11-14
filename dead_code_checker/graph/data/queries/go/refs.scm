; graph.extract.ts_generic uses for Go. Vocabulary: @ref.<kind>, @name the used name, @obj the receiver of a selector (name becomes
; obj.name; the bare name when obj is an imported package, both spellings when a local of that name shadows it), @local a name
; bound inside a function (see the end of the file), @import an import spec, @ref.exec a directive comment (go:generate).
; Pattern order does not matter here: a weak ref on a node that a call, inherit or def already uses is dropped by the mapping.

(call_expression function: (identifier) @name) @ref.call
(call_expression function: (selector_expression operand: (_) @obj field: (field_identifier) @name)) @ref.call
(call_expression function: (index_expression operand: (identifier) @name)) @ref.call

; type B struct { A }: embedding is inheritance (the scope is the struct); the other forms of an embedded field are at the end
(field_declaration !name type: (type_identifier) @name) @ref.inherit

; a name used as a value anywhere (a const, a var, a function: `return ErrX`, `n > Max`, `cb = handler`, `case Mode:`, f(h),
; []T{h}), x.Method as a method value, a named type used anywhere
(identifier) @name @ref.ref
(selector_expression operand: (_) @obj field: (field_identifier) @name) @ref.ref
(type_identifier) @name @ref.ref

(comment) @name @ref.exec
(import_spec) @import

; the other forms of an embedded field: pb.Unimplemented (a type of another package), Base[T], and the same behind a pointer
; (*Base, *pb.Unimplemented; the grammar may read the `*` as part of the field or as a pointer_type). The base is the bare
; type name: a base that is no type of the project is an external base, which keeps the methods of the struct
(field_declaration !name type: (qualified_type name: (type_identifier) @name)) @ref.inherit
(field_declaration !name type: (generic_type type: (type_identifier) @name)) @ref.inherit
(field_declaration !name type: (pointer_type (type_identifier) @name)) @ref.inherit
(field_declaration !name type: (pointer_type (qualified_type name: (type_identifier) @name))) @ref.inherit

; a name that a parameter, receiver, short declaration, var, range or type switch binds: inside its function it may shadow an
; imported package of the same name (`user := user.New(); user.Save()`), then x.f is a method call (rules ref_names)
(parameter_declaration name: (identifier) @local)
(variadic_parameter_declaration name: (identifier) @local)
(short_var_declaration left: (expression_list (identifier) @local))
(var_spec name: (identifier) @local)
(range_clause left: (expression_list (identifier) @local))
(type_switch_statement alias: (expression_list (identifier) @local))
(receive_statement left: (expression_list (identifier) @local))

; dep.F(T{..}), dep.F(&T{..}): a value of the type T handed to a function of an imported package may be used through an interface that this tree does not
; declare (a logger, a handler, a codec of a library), so any EXPORTED member of T may be called (members:T, D; graph.link: a package cannot call an unexported
; method through an interface, and a T that the tree does not declare keeps nothing). rules.json dyn.members.cmd_alias: the receiver is an imported package
(call_expression function: (selector_expression operand: (identifier) @cmd) arguments: (argument_list [(composite_literal type: (type_identifier) @dyn.members) (unary_expression operand: (composite_literal type: (type_identifier) @dyn.members))]))
