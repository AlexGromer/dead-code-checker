; graph.extract.ts_generic uses of TSX on top of typescript/refs.scm (query_alias): the JSX nodes the typescript grammar does not have.

; <Comp />, <ns.Comp>, onClick={handler}
(jsx_opening_element name: (_) @name) @ref.call
(jsx_self_closing_element name: (_) @name) @ref.call
(jsx_expression (identifier) @name) @ref.ref
