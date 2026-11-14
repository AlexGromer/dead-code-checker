# Pull request texts for curated lists (drafts, not submitted)

For each list: read its contribution rules the day you submit (alphabetical order, one line, minimum age or stars of the
project, category names change), and confirm the list is still maintained. A rejected submission is not retried.

Candidates to check, in order of fit:

1. A static-analysis tools list that has a per-language or "multiple languages" section.
2. A Python tools list, section on linters / code quality.
3. A list of pre-commit hooks, if one is maintained.
4. A GitHub Actions list, section on code quality, after the Action is listed.

## Entry line (adapt to each list's format)

> [dead-code-checker](https://github.com/AlexGromer/dead-code-checker) - Finds dead code that a commit or a pull request
> adds: unreferenced symbols and files, stubs, unread config keys. Deterministic, standard library only. Apache-2.0.

Shorter, for lists that want a few words:

> dead-code-checker - dead code in new changes; no model, no network.

## Pull request description

> **Add dead-code-checker**
>
> What it is: a command-line tool, pre-commit hook and GitHub Action (and an editor extension) that reports dead code in
> what a change adds.
>
> Why it fits this list: {{one sentence tied to the list's scope, for example "it supports Python, shell, JavaScript,
> Go and Rust out of the box and more with a flag"}}.
>
> Facts: open source (Apache-2.0), first release {{date}}, {{number}} release(s), documentation and a methodology page
> for the precision measurement at the link above.
>
> I am the author. I have read the contribution guidelines; the entry follows the list's order and format.

Keep "I am the author" in; a list that dislikes self-submission says so in its rules.
