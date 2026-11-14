; graph.extract.ts_generic typed variables of Java. Capture vocabulary: @var.<field|param|ctor|local> = the declaration, @type = its
; type node, @name = the variable, @mods = its modifiers (an @Autowired field). A declaration that matches twice (a constructor
; parameter is also a parameter) is read once, as the first kind of ts_generic.VAR_KINDS. The mapping uses them for the receiver
; of a call (field.m() -> Type.m) and for dependency injection (a field with an inject annotation, a constructor parameter,
; the parameters of a @Bean method -> a di ref to the type). A match without @type (a lambda parameter with an inferred type, a
; catch parameter) declares a variable of unknown type: it only hides a field of the same name.
(field_declaration (modifiers)? @mods type: (_) @type declarator: (variable_declarator name: (identifier) @name)) @var.field
(formal_parameter (modifiers)? @mods type: (_) @type name: (identifier) @name) @var.param
(constructor_declaration parameters: (formal_parameters (formal_parameter (modifiers)? @mods type: (_) @type name: (identifier) @name) @var.ctor))
(local_variable_declaration type: (_) @type declarator: (variable_declarator name: (identifier) @name)) @var.local
(enhanced_for_statement type: (_) @type name: (identifier) @name) @var.local
(lambda_expression parameters: (identifier) @name) @var.param
(lambda_expression parameters: (inferred_parameters (identifier) @name)) @var.param
(catch_formal_parameter name: (identifier) @name) @var.local
