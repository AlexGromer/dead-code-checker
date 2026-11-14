; graph.extract.ts_generic definitions for Rust. Vocabulary: @def.<kind>, @name, @recv (the type of the impl block: qual =
; Type.method), @vis the visibility_modifier (rules vis_cap), @flag.<name> a model flag of the definition (protocol: a trait impl
; method or const lives with its type; abstract: a trait method without a body). Pattern order is priority for one node: trait-impl
; items, then inherent items (methods, then consts), then the plain patterns. Associated items are members of the impl type
; (Type.CONST, called as Self::CONST / Type::CONST); an associated type (`type Output = Point;` in an impl) is no definition: it is
; named in signatures as the type it stands for, never as `Output`, so a type_item counts only at file or module level. Written
; from the node types of tree-sitter-rust, not yet run against an installed grammar (tests/test_graph_go_rust.py runs them when one is).

; impl Trait for T { fn m() }, T any type (the mapping names it: rules recv_sub): Foo, Foo<P>, &Foo, Box<dyn X>, f64, [T], m::Foo
(impl_item trait: (_) type: (_) @recv body: (declaration_list (function_item name: (identifier) @name) @def.method @flag.protocol))
; impl T { pub fn m() }
(impl_item type: (_) @recv body: (declaration_list (function_item (visibility_modifier)? @vis name: (identifier) @name) @def.method))

; associated consts: impl Trait for T { const ID: u32 = 1; } lives with its type, impl T { pub const MAX: u32 = 8; } is a member of T.
; A const of a trait is a member of the trait through the plain const pattern below (the trait is the enclosing definition)
(impl_item trait: (_) type: (_) @recv body: (declaration_list (const_item name: (identifier) @name) @def.const @flag.protocol))
(impl_item type: (_) @recv body: (declaration_list (const_item (visibility_modifier)? @vis name: (identifier) @name) @def.const))

(function_item (visibility_modifier)? @vis name: (identifier) @name) @def.func
(function_signature_item name: (identifier) @name) @def.func @flag.abstract
(struct_item (visibility_modifier)? @vis name: (type_identifier) @name) @def.class
(enum_item (visibility_modifier)? @vis name: (type_identifier) @name) @def.class
(trait_item (visibility_modifier)? @vis name: (type_identifier) @name) @def.iface
(source_file (type_item (visibility_modifier)? @vis name: (type_identifier) @name) @def.class)
(const_item (visibility_modifier)? @vis name: (identifier) @name) @def.const
(static_item (visibility_modifier)? @vis name: (identifier) @name) @def.var
(mod_item (visibility_modifier)? @vis name: (identifier) @name body: (declaration_list)) @def.module
(macro_definition name: (identifier) @name) @def.func
(mod_item body: (declaration_list (type_item (visibility_modifier)? @vis name: (type_identifier) @name) @def.class))
