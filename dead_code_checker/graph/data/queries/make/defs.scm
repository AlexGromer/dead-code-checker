; graph.extract.cfgts (make). Node names of tree-sitter-make (checked on the real grammar by tests/test_graph_langcfg.py).
; @rule a rule, @target one word of its targets (the mapping groups the words by the rule node).
(rule (targets (word) @target)) @rule
