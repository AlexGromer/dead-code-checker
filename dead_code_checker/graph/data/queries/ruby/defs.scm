; graph.extract.ts_generic definitions of Ruby. @def.<kind> = the whole definition, @name = its name (`A::B` is spelled A.B by rules.json name_map).

(class name: [(constant) (scope_resolution)] @name) @def.class
(module name: [(constant) (scope_resolution)] @name) @def.module
; def f, def ==(o), def name=(v): an operator and a setter are names too
(method name: [(identifier) (constant) (operator) (setter)] @name) @def.func
; def self.f: a method of the class that holds it
(singleton_method name: [(identifier) (constant) (operator) (setter)] @name) @def.func
; FOO = 1
(assignment left: (constant) @name) @def.const
