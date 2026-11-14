; graph.extract.cfgts (dockerfile). Node names of tree-sitter-dockerfile. @stage a FROM instruction, @image its image_spec,
; @alias its `AS name`.
(from_instruction (image_spec) @image (image_alias)? @alias) @stage
