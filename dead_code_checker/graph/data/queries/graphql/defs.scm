; graph.extract.schema (GraphQL). @def.<tag> = a definition (type | interface | input | enum | union) and @name its name, @ext.type = `extend type X`,
; @field = a field of an object or interface type (it is a definition when its type is a root operation type), @root = a root type that a
; `schema { query: X }` block names and @op its operation. No predicates.
(object_type_definition (name) @name) @def.type
(interface_type_definition (name) @name) @def.interface
(input_object_type_definition (name) @name) @def.input
(enum_type_definition (name) @name) @def.enum
(union_type_definition (name) @name) @def.union
(object_type_extension (name) @name) @ext.type
(field_definition (name) @name) @field
(root_operation_type_definition (operation_type) @op (named_type (name) @root))
