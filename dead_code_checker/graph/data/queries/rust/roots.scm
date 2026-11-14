; graph.extract.ts_generic roots for Rust. Vocabulary: @root.marker a comment (`// deadcode: root`), @root.cand a top-level function
; with its @name (data/rules.json roots: main), @root.attr an outer attribute with @attr the attribute itself (rules attrs read its
; path: #[test], #[tokio::main], #[no_mangle], #[get("/")] ..., and the paths wrapped in it, rules attr_wrap: #[unsafe(no_mangle)],
; #[cfg_attr(feature = "py", pyclass)]): the root is the definition that follows the attribute, @root.span an impl block (an
; attribute on it, #[pymethods], roots every method inside).
[(line_comment) (block_comment)] @root.marker
(source_file (function_item name: (identifier) @name) @root.cand)
(attribute_item (attribute) @attr) @root.attr
(impl_item) @root.span
