; graph.extract.ts_generic typed variables of Kotlin. Capture vocabulary: see java/types.scm.
; A property is a field in a class body and a local in a function (the scope decides): both are @var.field here.
(property_declaration (modifiers)? @mods (variable_declaration (simple_identifier) @name . [(user_type) (nullable_type)] @type)) @var.field
(class_parameter (modifiers)? @mods (simple_identifier) @name [(user_type) (nullable_type)] @type) @var.ctor
(parameter (simple_identifier) @name [(user_type) (nullable_type)] @type) @var.param
