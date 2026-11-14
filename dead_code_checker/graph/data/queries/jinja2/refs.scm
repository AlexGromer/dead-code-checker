; graph.extract.tpl (jinja2). Node names of tree-sitter-jinja2: the grammar is flat. @stmt: a `{% .. %}` statement (the mapping reads its
; keyword and arguments from the text), @call: a call written in a `{{ }}` output (`render(2)`, `m.thing(y)`).
(jinja_expression) @stmt
(fn_call) @call
