; graph.extract.ts_generic uses for Rust. Vocabulary: @ref.<kind>, @name, @obj (receiver of a method call: name becomes obj.name),
; @scope (the derived type of an impl or a supertrait: the Ref scope), @import (a use or `mod x;` declaration), @dyn (a
; macro_rules! definition: its expansion may call anything in the file, tier D), @member (the ref also matches members of that
; name), @attr (an outer attribute) with @attr.item (the whole attribute item): the attributes right before a `mod x;`
; declaration belong to its import (`#[path = "sys/unix.rs"] mod imp;`, rules mod_path), found by position like the attributes
; of a root. @item_macro with @item_body a macro call that may hold items (rules item_macros: cfg_if!), its body is read as text for
; `mod x;` and `use ..;`. Identifiers inside a macro call are raw tokens to the grammar, hence the weak token_tree ref, a method call
; included. A method call with a turbofish, `x.get::<T>()`, is a generic_function around the field_expression.

(call_expression function: (identifier) @name) @ref.call
(call_expression function: (scoped_identifier) @name) @ref.call
(call_expression function: (generic_function function: (identifier) @name)) @ref.call
(call_expression function: (generic_function function: (scoped_identifier) @name)) @ref.call
(call_expression function: (field_expression value: (_) @obj field: (field_identifier) @name)) @ref.call
(call_expression function: (generic_function function: (field_expression value: (_) @obj field: (field_identifier) @name))) @ref.call
(macro_invocation macro: (identifier) @name) @ref.call
(macro_invocation macro: (scoped_identifier) @name) @ref.call

; impl Trait for T, impl<P> Trait<P> for T<P>, trait A: B
(impl_item trait: (_) @name type: (_) @scope) @ref.impl
(trait_item name: (type_identifier) @scope bounds: (trait_bounds (type_identifier) @name)) @ref.inherit

; a name used as a value anywhere (a const, a static, a function, `Color::Red`, `utils::MAX`), tokens of a macro call, a named type
(identifier) @name @ref.ref
(scoped_identifier) @name @ref.ref
(token_tree (identifier) @name @member) @ref.ref
(type_identifier) @name @ref.ref
; a string in an attribute names a function: #[serde(default = "default_port")], deserialize_with, skip_serializing_if
(attribute (token_tree (string_literal) @name @ref.ref))

(use_declaration) @import
(mod_item name: (identifier) !body) @import
(macro_definition name: (identifier) @dyn)
; every outer attribute: the mapping gives those right before a `mod x;` to that import (rules mod_path), the others are dropped
(attribute_item (attribute) @attr) @attr.item
; the body of a macro that holds items (the mapping keeps the macros that rules item_macros names): `mod x;` and `use` inside it
(macro_invocation macro: (_) @item_macro (token_tree) @item_body)
