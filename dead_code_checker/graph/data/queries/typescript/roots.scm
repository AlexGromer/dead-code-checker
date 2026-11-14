; graph.extract.ts_generic roots of TypeScript: `// deadcode: root <why>` makes the definition on or just below that line a manual
; root. Entry points of a package are read from package.json (graph.roots, kind export / bin).
(comment) @root.marker

; a call at the top level of the file (`main();`, `main().catch(..)`, `(async () => { .. })();`, `await run();`): the file is a script, running it runs the
; statement (rules.json roots main-toplevel: soft; a test file and a package index are dropped by graph.link)
(program (expression_statement [(call_expression) (await_expression)] @root.main-toplevel))

; if (require.main === module) { .. }, if (import.meta.main) { .. }: the explicit script guard (rules.json roots main-guard, the same soft root)
(program (if_statement condition: (parenthesized_expression) @root.main-guard))
