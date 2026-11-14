; graph.extract.cfg (the Helm layer, _helm_tree). @call + @name: `{{ template "n" . }}` and the call that `{{ block "n" . }}` also is.
; @expr: the expressions of an action; the mapping keeps the outermost ones (a selector inside a chained pipeline is read with it).
(template_action . (interpreted_string_literal) @name) @call
(block_action . (interpreted_string_literal) @name) @call
[
  (function_call) (method_call) (selector_expression) (field) (variable) (dot) (chained_pipeline) (parenthesized_pipeline)
  (variable_definition) (range_variable_definition) (assignment)
] @expr
