; graph.extract.cfg (HCL). Written from the tree-sitter-hcl node names (config_file, body, block, attribute, identifier, string_lit,
; expression).
; The mapping groups the captures by the @block node: the first identifier is the block type, the string_lit children are the labels.
(block (identifier) @block.type) @block
(block (string_lit) @block.label) @block
; the attributes directly in a block body: `locals` defines them, `module` passes them to the variables of the called module
(block (identifier) @attr.kw (body (attribute (identifier) @attr.name) @attr.node)) @attr.block
