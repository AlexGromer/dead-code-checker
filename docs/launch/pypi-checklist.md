# PyPI checklist (draft, not submitted)

Package: `dead-code-checker`, version `{{version}}` (now `0.1.0` in `pyproject.toml`), Apache-2.0, Python 3.11 or newer,
no dependencies, console script `dead-code-checker`.

## Before the first upload

- [ ] The name is free on PyPI and on TestPyPI. Check it at upload time; do not assume.
- [ ] `pyproject.toml`: `version` equals `dead_code_checker.__version__` (a test checks it); `description`, `readme`,
      `license`, `requires-python`, `keywords` are set; add `classifiers` (Python versions, Apache license, "Software
      Development :: Quality Assurance") and the `[project.urls]` entries `Documentation`, `Issues`, `Changelog`.
- [ ] `[tool.setuptools] packages` lists every package directory and `package-data` includes the graph data files
      (a test fails when a package directory is missing from the list).
- [ ] The README renders as the PyPI long description: no relative image links, and relative links to docs become
      absolute.
- [ ] `CHANGELOG.md`: the `Unreleased` section becomes the version and the date.
- [ ] A scan of the tree for anything private (local paths, host names, tokens, personal data) and a secret scan; the
      public tree must contain neither.
- [ ] The Action placeholder `PIN_BY_SHA` in `action.yml` is replaced by a reviewed commit SHA (see docs/ci.md), if the
      same tag is also the Action release.
- [ ] `python -m build` produces one sdist and one wheel; `twine check dist/*` passes; install the wheel into a clean
      virtual environment and run `dead-code-checker defaults` and `dead-code-checker graph doctor`.

## Upload

- [ ] Upload to TestPyPI first, install from it in a clean environment, run the two commands above.
- [ ] On PyPI, set up a trusted publisher (OpenID Connect from GitHub Actions) for the repository and a release workflow
      environment, instead of a long-lived API token. If a token is used, scope it to this project only and keep it as a
      repository secret.
- [ ] Tag `v{{version}}`, publish from the workflow, check the project page.
- [ ] Two-factor authentication is on for every maintainer account.

## After

- [ ] `pipx install dead-code-checker` in a clean environment works; the README install section says that (the git URL
      stays as the "development version" line).
- [ ] The pre-commit hook `rev` in the README matches the tag.
- [ ] A yank procedure is written down (who, when): a release that breaks the pre-commit hook is yanked, not deleted.
