; graph.extract.ts_generic uses of Java. Capture vocabulary: @ref.<kind> = the use (call | ref | inherit | impl), @name = the used
; name, @recv = the receiver expression of a call (lang_rules.qualify turns it into "T.m" for a variable of a known type).
; The base of a class is a use whose scope is the DERIVED class: extends -> inherit, implements -> impl.

; foo(x): no receiver (also tried as this.foo, an inherited member)
(method_invocation !object name: (identifier) @name) @ref.call
; a.foo(x), this.a.foo(x), Type.foo(x), a.b().foo(x)
(method_invocation object: (_) @recv name: (identifier) @name) @ref.call
; Type::foo, this::foo
(method_reference . (_) @recv . (identifier) @name .) @ref.ref
; Type::new, Type::foo: the qualifier is a use of the type (a variable of that name links to nothing)
(method_reference . (identifier) @name) @ref.ref
; Constants.MAX, Status.ACTIVE, a.b.Foo.BAR: the first name of a field access is a use of a type (or of a variable: links to nothing)
(field_access object: (identifier) @name) @ref.ref
; new Foo(x), new Foo<>(x)
(object_creation_expression type: (type_identifier) @name) @ref.ref
(object_creation_expression type: (generic_type (type_identifier) @name)) @ref.ref
; every other use of a type: declarations, casts, generics, Foo.class
(type_identifier) @name @ref.ref
; @Foo, @Foo(...): the annotation type. A use of kind reg: a project annotation type hands its own rules on to the definition that carries it
(marker_annotation name: (_) @name) @ref.reg
(annotation name: (_) @name) @ref.reg
; class A extends B implements C, D / interface I extends J
(superclass (type_identifier) @name) @ref.inherit
(superclass (generic_type (type_identifier) @name)) @ref.inherit
(super_interfaces (type_list (type_identifier) @name)) @ref.impl
(super_interfaces (type_list (generic_type (type_identifier) @name))) @ref.impl
(extends_interfaces (type_list (type_identifier) @name)) @ref.inherit
(extends_interfaces (type_list (generic_type (type_identifier) @name))) @ref.inherit

; Class.forName("p.Plugin"), loader.loadClass("p.Plugin"), c.getMethod("run"): a class or a member named by a string literal (rules.json dyn.name
; filters the command and reads the simple name: the D-tier site `name:Plugin`, every def of that name may be used by reflection)
(method_invocation name: (identifier) @cmd arguments: (argument_list . (string_literal) @dyn.name))
