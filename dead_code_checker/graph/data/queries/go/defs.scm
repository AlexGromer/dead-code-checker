; graph.extract.ts_generic definitions for Go. Vocabulary: @def.<kind> the whole definition, @name its identifier, @recv the receiver
; type of a method (qual = Type.method). Pattern order is priority when several patterns match one node: the receiver patterns
; come first and claim a method before the plain function pattern. Written from the node types of tree-sitter-go, not yet run
; against an installed grammar (tests/test_graph_go_rust.py runs them when one is).

; func (r T) m() {}, func (r *T) m() {}, and the same on a generic receiver T[P]
(method_declaration receiver: (parameter_list (parameter_declaration type: (type_identifier) @recv)) name: (field_identifier) @name) @def.method
(method_declaration receiver: (parameter_list (parameter_declaration type: (pointer_type (type_identifier) @recv))) name: (field_identifier) @name) @def.method
(method_declaration receiver: (parameter_list (parameter_declaration type: (generic_type type: (type_identifier) @recv))) name: (field_identifier) @name) @def.method
(method_declaration receiver: (parameter_list (parameter_declaration type: (pointer_type (generic_type type: (type_identifier) @recv)))) name: (field_identifier) @name) @def.method

(function_declaration name: (identifier) @name) @def.func

; type T interface { M() }: an iface (the CHA candidates), its methods (method_elem, formerly method_spec) are members; type T struct {...}; any other named type
(type_spec name: (type_identifier) @name type: (interface_type)) @def.iface
(type_spec name: (type_identifier) @name type: (struct_type)) @def.class
(type_spec name: (type_identifier) @name) @def.class
(interface_type (_ name: (field_identifier) @name) @def.func)

; package level const and var, a single spec or a group `const ( ... )`: the specs are children of the declaration or of a list node
; under it (the wildcard keeps the pattern valid whichever the grammar version calls that list)
(source_file (const_declaration [(const_spec name: (identifier) @name) @def.const (_ (const_spec name: (identifier) @name) @def.const)]))
(source_file (var_declaration [(var_spec name: (identifier) @name) @def.var (_ (var_spec name: (identifier) @name) @def.var)]))
