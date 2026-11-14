; graph.extract.ts_generic definitions of Scala. @def.<kind> = the whole definition, @name = its name, @mod = a modifier word (rules.json vis_mods, flag_mods).
; The plain pattern of a definition claims the node; the patterns with a modifier only add the @mod of the same node.

(class_definition name: (identifier) @name) @def.class
(object_definition name: (identifier) @name) @def.class
(enum_definition name: (identifier) @name) @def.class
(trait_definition name: (identifier) @name) @def.iface
(function_definition name: (identifier) @name) @def.func
(function_declaration name: (identifier) @name) @def.func
(val_definition pattern: (identifier) @name) @def.var
(var_definition pattern: (identifier) @name) @def.var
(function_definition (modifiers (access_modifier) @mod) name: (identifier) @name) @def.func
(function_definition (modifiers "override" @mod) name: (identifier) @name) @def.func
(function_declaration (modifiers (access_modifier) @mod) name: (identifier) @name) @def.func
(class_definition (modifiers (access_modifier) @mod) name: (identifier) @name) @def.class
(object_definition (modifiers (access_modifier) @mod) name: (identifier) @name) @def.class
(val_definition (modifiers (access_modifier) @mod) pattern: (identifier) @name) @def.var
; an implicit definition (Scala 2) or a named given (Scala 3) carries the flag implicit (rules.json flag_mods; @flag.implicit): an import brings it
; into scope and it is used without being named (rules.json dyn.implicit, refs.scm). A given with a body is a class, an alias given a value.
(function_definition (modifiers "implicit" @mod) name: (identifier) @name) @def.func
(val_definition (modifiers "implicit" @mod) pattern: (identifier) @name) @def.var
(var_definition (modifiers "implicit" @mod) pattern: (identifier) @name) @def.var
(class_definition (modifiers "implicit" @mod) name: (identifier) @name) @def.class
(object_definition (modifiers "implicit" @mod) name: (identifier) @name) @def.class
(given_definition name: (identifier) @name @flag.implicit body: (with_template_body)) @def.class
(given_definition name: (identifier) @name @flag.implicit) @def.var
