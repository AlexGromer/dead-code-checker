# GitHub Marketplace listing (draft, not submitted)

Prerequisites: `action.yml` at the repository root with `name`, `description` and `branding` (present); the name is
unique on the Marketplace (check at submission); the repository is public; the `PIN_BY_SHA` placeholder is replaced by a
reviewed commit SHA; a release is tagged; the maintainer account has two-factor authentication and has accepted the
Marketplace developer agreement.

## Listing

- **Name:** Dead Code Checker
- **Category (primary):** Code quality. **Secondary:** Code review. Add Security only if it does not look like a stretch
  (the action uploads SARIF to code scanning).
- **Icon / colour:** `search`, `gray-dark` (from `action.yml`).
- **Short description (one line):** Find dead code that a pull request adds; SARIF to code scanning, one PR comment, fails only on errors.
- **Long description:**

  > dead-code-checker looks at what a change adds and asks one question: can anything reach it? A function nobody calls,
  > a file nobody imports, a hook that ignores its input and prints a constant, a config key nobody reads.
  >
  > The action checks the pull request against its base, uploads the findings to code scanning as SARIF, posts or updates
  > one summary comment, writes a shields.io badge file, and fails the job only on findings of level E. Warnings inform
  > and never fail. Deterministic: no model, no network access during analysis, standard library only.
  >
  > Needs Python 3.11 or newer on the runner and `fetch-depth: 0` on checkout. Permissions: `contents: read`,
  > `security-events: write`, `pull-requests: write`. See docs/ci.md for inputs and for pinning.

- **Usage snippet:** the quick start of [docs/ci.md](../ci.md).

## Release steps

- [ ] Tag `v{{version}}` and a moving major tag (`v1`) that points at it, so users can choose between a SHA and a major.
- [ ] Draft a GitHub release from the tag, tick "Publish this Action to the GitHub Marketplace", choose the categories.
- [ ] Run the action in a throwaway public repository on a pull request with one seeded E and one seeded W: the job must
      fail on the E, the comment must show both, code scanning must list the alerts, the badge file must be red.
- [ ] Then the same pull request with the E removed: the job passes, the comment is edited, the badge is yellow.
