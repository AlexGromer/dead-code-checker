; graph.extract.ts_generic uses of JavaScript. @ref.<kind> = the use, @name = the used name (a dotted name for a member), @dyn = a
; dynamic site. No predicates: the mapping drops a name that is not a plain dotted identifier (a call on a call).

; f(), a.b.c(), new C(), new ns.C()
(call_expression function: (identifier) @name) @ref.call
(call_expression function: (member_expression) @name) @ref.call
(new_expression constructor: (identifier) @name) @ref.call
(new_expression constructor: (member_expression) @name) @ref.call

; <Comp />, <ns.Comp>, onClick={handler}
(jsx_opening_element name: (_) @name) @ref.call
(jsx_self_closing_element name: (_) @name) @ref.call
(jsx_expression (identifier) @name) @ref.ref

; an identifier passed, returned, assigned, stored: a weak ref (tier N, by name)
(arguments (identifier) @name) @ref.ref
(pair value: (identifier) @name) @ref.ref
(shorthand_property_identifier) @name @ref.ref
(array (identifier) @name) @ref.ref
(assignment_expression right: (identifier) @name) @ref.ref
(variable_declarator value: (identifier) @name) @ref.ref
(return_statement (identifier) @name) @ref.ref

; a member used as a value: passed, returned, stored, read (`on("x", this.h)`, `arr.map(this.render)`, `{this.h}`, `o.value` of an
; accessor): a weak ref by the member name. @mem lets the mapping drop the member that is a callee or the object of a longer one.
(member_expression) @name @mem @ref.ref

; an identifier as an operand: `c ? a : b`, `x || fn`, `...fn`, `(fn)`, `await p`, `!fn`, `(a, fn)`, `() => fn`, `@Dec`, `${fn}`, `o[fn]`,
; `(cb = dflt)`, `x += fn`, `yield fn`, `throw e`, `case K:`, `for (x of fn)`, `h = fn` (a class field)
(ternary_expression (identifier) @name) @ref.ref
(binary_expression (identifier) @name) @ref.ref
(spread_element (identifier) @name) @ref.ref
(parenthesized_expression (identifier) @name) @ref.ref
(await_expression (identifier) @name) @ref.ref
(unary_expression (identifier) @name) @ref.ref
(sequence_expression (identifier) @name) @ref.ref
(arrow_function body: (identifier) @name) @ref.ref
(decorator (identifier) @name) @ref.ref
(template_substitution (identifier) @name) @ref.ref
(subscript_expression (identifier) @name) @ref.ref
(assignment_pattern right: (identifier) @name) @ref.ref
(augmented_assignment_expression right: (identifier) @name) @ref.ref
(yield_expression (identifier) @name) @ref.ref
(throw_statement (identifier) @name) @ref.ref
(switch_case value: (identifier) @name) @ref.ref
(for_in_statement right: (identifier) @name) @ref.ref
(field_definition value: (identifier) @name) @ref.ref

; class A extends B / ns.B (the scope of the ref is the derived class)
(class_declaration (class_heritage (identifier) @name)) @ref.inherit
(class_declaration (class_heritage (member_expression) @name)) @ref.inherit
; a class expression (`export default class extends B {}`, `const A = class extends B {}`): no def of its own, the scope of the ref is the
; enclosing definition (the file at the top level), so the base stays used
(class (class_heritage (identifier) @name)) @ref.inherit
(class (class_heritage (member_expression) @name)) @ref.inherit

; obj[key](...): the callee is only known at run time (tier D)
(call_expression function: (subscript_expression) @dyn)
