; graph.extract.ts_generic uses of C#. Vocabulary: @ref.<kind>, @name, @cmd (a generic call's method name, filtered by rules.json ref_cmd).
(invocation_expression function: (identifier) @name) @ref.call
(invocation_expression function: (member_access_expression) @name) @ref.call
(invocation_expression function: (generic_name (identifier) @name)) @ref.call
(invocation_expression function: (member_access_expression name: (generic_name (identifier) @name))) @ref.call
(object_creation_expression type: (identifier) @name) @ref.call
(object_creation_expression type: (qualified_name) @name) @ref.call
(object_creation_expression type: (generic_name (identifier) @name)) @ref.call

; x?.Run() / x?.Prop: the member of a null-conditional access has no receiver text of its own; new Foo { Id = 1 }: a property set by name in an initializer.
; @member makes the ref "?.Run": link.py resolves it to every member of that name
(invocation_expression function: (conditional_access_expression (member_binding_expression name: (identifier) @member))) @ref.call
(member_binding_expression name: (identifier) @member) @ref.ref
(member_binding_expression name: (generic_name (identifier) @member)) @ref.ref
(initializer_expression (assignment_expression left: (identifier) @member)) @ref.ref

; services.AddScoped<IFoo, Foo>(): the registration makes the implementation reachable (a DI edge to every type argument)
(invocation_expression function: (member_access_expression name: (generic_name (identifier) @cmd (type_argument_list (identifier) @name)))) @ref.di

; class C : Base, IFoo: the scope of the match is the derived type (base_list lies inside its span); CHA/DI are link.py
(base_list (identifier) @name) @ref.inherit
(base_list (generic_name (identifier) @name)) @ref.inherit
(base_list (qualified_name) @name) @ref.inherit

; Every other use is weak and position-free, so no form of C# can hide one: a field read in `if (_flag)`, foreach, lock, `_arr[i]`, `_x++` or an
; interpolation; a type in a return type, `Foo[]`, `Foo?`, `is`/`as`, `catch (FooException)`, a foreach variable, a `where T : Foo` constraint,
; `default(T)`, typeof, a cast or an attribute (all plain identifiers in this grammar); a method group through a member access
; (`list.Select(Util.Parse)`, `Task.Run(Worker.Process)`, `x.Click += obj.OnClick`: the dotted text resolves to a member of that name).
; The identifier that DECLARES a definition is skipped by ts_generic (no self edge). Cost: a local or a parameter that shares its name with
; a member counts as a use of it (over-alive, never a false dead).
(identifier) @name @ref.ref
(member_access_expression) @name @ref.ref

; using static Ns.Util;: the members of Util are used by their bare name (rules.json dyn.static reads the directive: `static:Util`, D tier)
(using_directive) @dyn.static

; type.GetMethod("Run"), GetProperty("Name"), InvokeMember, GetType("Ns.Foo"), Activator.CreateInstance("asm", "Ns.Foo"): a member or a type named by a
; string literal (rules.json dyn.name filters the command and reads the name: `name:Run`, D tier)
(invocation_expression function: (member_access_expression name: (identifier) @cmd) (argument_list (argument (string_literal) @dyn.name)))

; app.UseMiddleware<Mw>(), app.UseMiddleware<Ns.Mw>(a), app.UseMiddleware(typeof(Mw)): the host builds Mw and calls its Invoke / InvokeAsync by
; convention (no base type, no interface), so the members of Mw may be used by their bare name (rules.json dyn.members: `members:Mw`, D tier)
(invocation_expression function: (member_access_expression name: (generic_name (identifier) @cmd (type_argument_list (_) @dyn.members))))
(invocation_expression function: (member_access_expression name: (identifier) @cmd) (argument_list (argument (typeof_expression (_) @dyn.members))))
