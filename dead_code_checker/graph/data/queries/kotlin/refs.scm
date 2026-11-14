; graph.extract.ts_generic uses of Kotlin. Capture vocabulary: see java/refs.scm.
; foo(x), Foo(x): no receiver (a constructor call is a call of the class name)
(call_expression (simple_identifier) @name) @ref.call
; a.foo(x), this.a.foo(x), Type.foo(x)
(call_expression (navigation_expression (_) @recv (navigation_suffix (simple_identifier) @name))) @ref.call
; Foo.CONST, Foo.bar: the first name of a navigation is a use of a type (or of a variable: links to nothing)
(navigation_expression . (simple_identifier) @name) @ref.ref
; Foo::class, Foo::bar, ::fn
(callable_reference (type_identifier) @name) @ref.ref
(callable_reference (simple_identifier) @name) @ref.ref
; every use of a type
(user_type (type_identifier) @name) @ref.ref
; every plain name: a top-level property or constant (println(SETTINGS), x = LIMIT), a function value. The name of a definition and the
; names of an import are dropped by the mapping; a name that resolves to nothing in the project adds no edge.
(simple_identifier) @name @ref.ref
; @Foo, @Foo(...): the annotation type. A use of kind reg: a project annotation type hands its own rules on to the definition that carries it
(annotation (user_type (type_identifier) @name)) @ref.reg
(annotation (constructor_invocation (user_type (type_identifier) @name))) @ref.reg
; class A : B(), C: the bases are uses whose scope is the derived class
(delegation_specifier (user_type (type_identifier) @name)) @ref.inherit
(delegation_specifier (constructor_invocation (user_type (type_identifier) @name))) @ref.inherit

; Class.forName("p.Plugin"), loader.loadClass("p.Plugin"), c.getMethod("run"): see queries/java/refs.scm (rules.json dyn.name)
(call_expression (navigation_expression (navigation_suffix (simple_identifier) @cmd)) (call_suffix (value_arguments . (value_argument (string_literal) @dyn.name))))
