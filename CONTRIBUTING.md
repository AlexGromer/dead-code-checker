# Contributing to dead-code-checker

Thanks for your interest in dead-code-checker. This page describes how to propose a change so that it can
be reviewed and merged quickly.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

- **Discuss first for anything large.** Open an issue before a big change, so the direction is agreed
  before you spend time on it. Small fixes (typos, docs, an obvious bug) can go straight to a PR.
- **One logical change per pull request.** Keep the diff focused; unrelated cleanups go in their own PR.
- **Tests and docs travel with the code.** A behaviour change comes with a test that fails without
  it, and with an update of the documentation it touches.
- **No secrets.** Never commit `.env` files, keys, tokens or personal data. Run a secret scanner
  (for example `gitleaks detect`) before you push.

## Workflow

1. Fork the repository and create a branch from `main`: `feat/short-name`, `fix/short-name`,
   `docs/short-name`.
2. Make the change and run the project's build and test commands (see the README).
3. Commit with a clear message (see below).
4. Open a pull request and fill in the template. Link the issue it closes.
5. Respond to review comments; push follow-up commits to the same branch.

`main` is protected: changes land through reviewed pull requests only.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, imperative
mood, present tense. Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, `build`, `perf`.

```
fix(parser): handle an empty input file
```

Signed commits are welcome. Keep the working tree green: every commit should build and pass the tests.

## Reporting bugs and requesting features

Use the issue templates. Search existing issues first. For a bug, give the version or commit,
the exact steps to reproduce, what you expected and what happened.

## Security

Do **not** open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md) for private reporting.

## License

By contributing you agree that your contribution is licensed under the Apache License 2.0.
