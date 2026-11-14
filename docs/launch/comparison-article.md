# Comparison article outline: dead-code-checker vs vulture, knip, fallow (draft, not written)

Working title: "Dead code tools compared on the same repositories".

Ground rules, because a comparison written by one of the contestants is easy to distrust:

1. **Claims about dead-code-checker come only from this repository's documents** (README, docs/limits.md,
   docs/v2.md, docs/methodology.md). Each claim in the article links to the line it comes from.
2. **Claims about the other tools come only from their own documentation, read on the day of writing**, quoted with the
   version and the date. Anything that is not in their documentation is stated as "not tested here". Nothing is taken from
   memory; this outline lists topics, not statements.
3. **Numbers come from one run** of the procedure in docs/methodology.md: the same sample of repositories, the same
   definition of a true finding, each tool with its default configuration and, separately, with the configuration its
   documentation recommends for the project type. Findings of a kind that only one tool reports are compared only where
   the kinds overlap.
4. **Fill the numbers last.** Every figure below is `{{n}}` until the run exists. If a result is unflattering to
   dead-code-checker, it stays in.
5. **Offer the other maintainers the draft** before publishing, and take their corrections about how their tool should be
   run.

## Outline

1. **What "dead" means** (short). Unused vs unreachable; entry points; why libraries and applications differ
   (`mode = "closed_world"` or `"library"` here). Link to docs/v2.md.
2. **The contestants**, one paragraph each, from their documentation: what language(s), what it looks at, how it is run,
   how it is configured. Only the facts needed to read the table.
   - vulture: {{from its documentation: languages, approach, confidence scores, whitelist mechanism}}
   - knip: {{from its documentation: languages and ecosystems, what it reports, plugins}}
   - fallow: {{from its documentation, after checking that it exists in the form assumed and what it covers}}
   - dead-code-checker: Python, shell, JavaScript, Go, Rust by default, more with a flag; the text checker and the graph
     checker; a branch scope that looks only at what a change adds.
3. **Method.** The sample (disjoint, seeded), the labelling (blind, two labellers, kappa), the interval (Wilson), the
   configurations used. Link to docs/methodology.md. State what was excluded and why.
4. **Results.**
   - Table: tool x language: findings, labelled n, true k, precision with interval, unclear.
   - Table: the same on the findings of the branch scope only (what a change adds), where the tools can be run that way.
   - Time and memory on the same repositories, same machine, cold and warm, with the command lines.
   - Agreement between tools: how many true findings are found by only one tool (this is the closest thing to recall that
     can be stated without knowing the total).
5. **Why the false findings happen.** The causes grouped (dynamic use, framework conventions, public API, generated code,
   tests), per tool, with one real example each. This is the section readers will use.
6. **Where each tool is the better choice**, written from the results, not from the outline. Expect at least one case
   where it is not dead-code-checker.
7. **Using them together.** Whether and how the outputs combine; the pre-commit and CI shapes for each.
8. **Reproduce it.** The list of repositories with commit ids, the command lines, the labelled file, the seed.

## Do not include

- Marketing adjectives, "faster", "smarter", "more accurate" without the table next to them.
- Statements about another tool's roadmap, adoption or quality that its documentation does not make.
- Results from a tool configuration nobody would use.

## Venue

dev.to and a personal blog first; submit to Hacker News and Habr only after the maintainers of the compared tools have
had the draft. The labelled file and the run script go into the repository so the article stays checkable.
