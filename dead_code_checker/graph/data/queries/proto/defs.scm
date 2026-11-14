; graph.extract.schema (proto). @pkg = the package name, @def.<tag> = a definition (message | enum | service | rpc) and @name its name. No predicates.
(package (full_ident) @pkg)
(message (message_name) @name) @def.message
(enum (enum_name) @name) @def.enum
(service (service_name) @name) @def.service
(rpc (rpc_name) @name) @def.rpc
