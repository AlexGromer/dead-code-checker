; graph.extract.cfgts (make). @rule + @prereq: a prerequisite (normal and order-only: a word, `$(VAR)`, `a-$(X)`, a function call); @path: a file of an include / -include
; (a $(wildcard ..) pattern is its text); @cmd a recipe line (it may run `$(MAKE) x`); @var + @val an assignment (.DEFAULT_GOAL).
(rule (prerequisites (_) @prereq)) @rule
(include_directive (list (word) @path))
(include_directive (list (function_call (arguments (text) @path))))
(recipe_line (shell_text) @cmd)
(variable_assignment (word) @var (text) @val)
