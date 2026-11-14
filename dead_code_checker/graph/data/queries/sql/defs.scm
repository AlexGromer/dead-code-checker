; graph.extract.schema (SQL). @def.<tag> = a definition (tag = table | view | function | procedure) and @name its object_reference (the first one
; wins: a schema-qualified name is one object_reference). No predicates.
(create_table (object_reference) @name) @def.table
(create_view (object_reference) @name) @def.view
(create_materialized_view (object_reference) @name) @def.view
(create_function (object_reference) @name) @def.function
(create_procedure (object_reference) @name) @def.procedure
