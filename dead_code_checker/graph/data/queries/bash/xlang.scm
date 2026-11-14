; graph.extract.xlang (cross-language facts). Vocabulary: @cmd a command node (its text is one command line for graph.roots.command_targets: a
; script path, `python3 x.py`, `env X=1 tool`), @lit a string or word that may spell a path, @env.w / @env.r with @name a variable that
; is exported / read. No predicates: the mapping filters by text.
(command) @cmd
(string) @lit
(raw_string) @lit
(command (word) @lit)
(declaration_command (variable_assignment name: (variable_name) @name)) @env.w
(declaration_command (variable_name) @name) @env.w
(command (variable_assignment name: (variable_name) @name)) @env.w
(simple_expansion (variable_name) @name) @env.r
(expansion (variable_name) @name) @env.r
