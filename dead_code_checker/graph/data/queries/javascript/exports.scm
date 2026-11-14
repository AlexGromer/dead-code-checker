; graph.extract.ts_generic exports of JavaScript. @export = the statement, @name = a local name that is exported (+ @alias = the
; name it is exported as), @default = the local name exported as default, @obj / @prop = the target of a CommonJS assignment
; (the mapping keeps module.exports = x, module.exports = { .. }, module.exports.k = x, exports.x = y, Object.assign(module.exports, { .. })),
; @key = the property assigned on module.exports, @fn = the callee of Object.assign. `export .. from` is read by imports.scm.

; export function f, export class C, export const x = 1, export var y = 2, export { a, b as c }
(export_statement declaration: (_ name: (_) @name)) @export
(export_statement declaration: (lexical_declaration (variable_declarator name: (identifier) @name))) @export
(export_statement declaration: (variable_declaration (variable_declarator name: (identifier) @name))) @export
(export_statement (export_clause (export_specifier name: (_) @name alias: (_)? @alias))) @export

; export default foo;  export default function foo() {}
(export_statement "default" value: (identifier) @default) @export
(export_statement "default" declaration: (_ name: (_) @default)) @export

; module.exports = foo;  exports.x = foo;  module.exports = { a, b: c };  module.exports.k = foo;  Object.assign(module.exports, { a, b: c })
(assignment_expression left: (member_expression object: (identifier) @obj property: (property_identifier) @prop) right: (identifier) @default) @export
(assignment_expression left: (member_expression object: (identifier) @obj property: (property_identifier) @prop) right: (object (shorthand_property_identifier) @name)) @export
(assignment_expression left: (member_expression object: (identifier) @obj property: (property_identifier) @prop) right: (object (pair key: (property_identifier) @alias value: (identifier) @name))) @export
(assignment_expression left: (member_expression object: (member_expression object: (identifier) @obj property: (property_identifier) @prop) property: (property_identifier) @key) right: (identifier) @default) @export
(call_expression function: (_) @fn arguments: (arguments (member_expression object: (identifier) @obj property: (property_identifier) @prop) (object (shorthand_property_identifier) @name))) @export
(call_expression function: (_) @fn arguments: (arguments (member_expression object: (identifier) @obj property: (property_identifier) @prop) (object (pair key: (property_identifier) @alias value: (identifier) @name)))) @export

; module.exports = new Foo();  export default new Foo();  (the module value is an instance: it uses Foo, and what is read from it is a member)
(assignment_expression left: (member_expression object: (identifier) @obj property: (property_identifier) @prop) right: (new_expression constructor: (identifier) @default)) @export
(export_statement "default" value: (new_expression constructor: (identifier) @default)) @export
