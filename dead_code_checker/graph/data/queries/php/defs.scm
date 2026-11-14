; graph.extract.ts_generic definitions of PHP. @def.<kind> = the whole definition, @name = its name, @mod = a modifier word (rules.json vis_mods, flag_mods).
; A definition is matched by the first pattern (plain); the patterns with a modifier only add the @mod of the same node.

(function_definition name: (name) @name) @def.func
(class_declaration name: (name) @name) @def.class
(interface_declaration name: (name) @name) @def.iface
(trait_declaration name: (name) @name) @def.class
(enum_declaration name: (name) @name) @def.class
(method_declaration name: (name) @name) @def.func
(method_declaration (visibility_modifier) @mod name: (name) @name) @def.func
(method_declaration (abstract_modifier) @mod name: (name) @name) @def.func
(const_declaration (const_element (name) @name)) @def.const
