; graph.extract.ts_generic definitions of Groovy. The grammar is loose: every statement is a `command` of `unit`s, and a class has no node of its own.
; @kw = the keyword word before the name (rules.json def_kw says which words define a class or an interface), @mod = a modifier word.

; class Base, abstract class Base extends A implements B, trait T, enum E
(command (unit (identifier) @kw) . (unit (identifier) @name)) @def.class
; class Tools { .. }: with no extends / implements the name is the first unit of the body, as for an interface
(command (unit (identifier) @kw) . (block . (unit (identifier) @name))) @def.class
; interface Greeter { ... }
(command (unit (identifier) @kw) . (block . (unit (identifier) @name))) @def.iface
; def run() { .. }, private hidden() { .. }, String greet(String n) { .. }: modifiers or a type, then a body that starts with the signature
(command (unit) . (block . (unit (func . (identifier) @name)))) @def.func
(command (unit (identifier) @mod) (unit) . (block . (unit (func . (identifier) @name)))) @def.func
(command (unit (identifier) @mod) . (block . (unit (func . (identifier) @name)))) @def.func
