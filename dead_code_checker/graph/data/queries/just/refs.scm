; graph.extract.cfgts (just). @dep: a dependency of a recipe (`b`, `(b "arg")`), @recipe the recipe holding it; @cmd a recipe line (it
; may run `just x` or `just {{ t }}`: the whole line, interpolations too); @alias + @target: `alias a := b`; @path: the file of an `import`; @mod + @path: `mod name 'dir'`.
(recipe (recipe_header (dependencies (dependency (identifier) @dep)))) @recipe
(recipe (recipe_header (dependencies (dependency (dependency_expression . (identifier) @dep))))) @recipe
(recipe_line) @cmd
(alias (identifier) @alias . (identifier) @target)
(import (string) @path)
(module (identifier) @mod)
(module (identifier) @mod (string) @path)
