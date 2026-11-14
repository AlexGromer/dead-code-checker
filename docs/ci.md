# Continuous integration

dead-code-checker ships a composite GitHub Action. On a pull request it checks what the change adds, uploads the result
as code scanning alerts, posts one summary comment, writes a badge file, and fails the job only when there is at least one
finding of level E. Warnings (W) never fail it.

It calls the graph checker: `dead-code-checker graph check --scope branch|repo --format sarif|json`.

## Quick start

```yaml
name: dead-code
on:
  pull_request:
permissions:
  contents: read
  security-events: write     # SARIF upload
  pull-requests: write       # the summary comment
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<full commit sha>
        with:
          fetch-depth: 0     # the branch scope needs the base commit
      - uses: AlexGromer/dead-code-checker@<full commit sha or release tag>
```

The runner needs Python 3.11 or newer; `ubuntu-latest` has it. The action installs the checker with `pip` from its own
checkout, so the version of the action is the version of the checker.

## Inputs

| input | default | meaning |
|---|---|---|
| `scope` | `branch` | `branch` checks what the change adds against its base; `repo` checks the whole repository |
| `base` | pull request base, else the commit before a push | the commit or ref the branch scope compares with |
| `working-directory` | `.` | repository directory, relative to the workspace |
| `package` | the action's own checkout | what pip installs, for example `dead-code-checker==0.2.0` to pin a release |
| `upload-sarif` | `true` | upload the SARIF report to code scanning |
| `sarif-category` | `dead-code-checker` | category of the upload; give each job of a matrix its own |
| `comment` | `true` | post or update the summary comment on pull requests |
| `badge-file` | `dead-code-badge.json` | where the badge endpoint JSON is written |

## Outputs

`errors`, `warnings` (counts), `sarif-file` and `badge-file` (paths).

## What fails the job

The last step of the action reads the exit code of the checker, and nothing else decides the result.

| exit code of the checker | job |
|---|---|
| 0, clean or warnings only | passes |
| 1, at least one E | fails, `::error::` names the count |
| anything else (a usage error, a setup error) | fails, so a broken setup is never a silent pass |

The check step only records what the checker said, so the report is still uploaded and the comment is still posted when
there is an E. The upload and the comment are advisory: neither can fail the job (see "Code scanning" and "The pull
request comment"). `tests/test_dist_scripts.py` runs the steps of `action.yml` against a stand-in checker and covers
each row of the table, a refused upload and a pull request from a fork.

The table is the exit-code contract the action expects from `dead-code-checker graph check`: 0 no E, 1 at least one E,
2 a usage or setup error, as in the checker's other commands. The action only reads the code. What the checker does when
it cannot finish an analysis level in its time budget is the checker's part of that contract, not the action's; the
budgets are listed in [docs/v2.md](v2.md).

The action runs the checker twice on the same commit: once with `--format json` (the exit code and the comment come from
it) and once with `--format sarif` (code scanning). The second run is skipped when nothing will upload it
(`upload-sarif: false`, or a pull request from a fork). If the analysis is not deterministic for a commit, the alerts and
the verdict can differ; the verdict is the JSON run.

## Pinning the actions

A workflow that uses a third-party action by a moving tag runs whatever that tag points to tomorrow. Pin every
`uses:` to the full 40-character commit SHA of a release you have looked at, and keep the release name in a comment.

`action.yml` uses one third-party action, `github/codeql-action/upload-sarif`. In the source tree it reads
`github/codeql-action/upload-sarif@PIN_BY_SHA`. **This placeholder is deliberate and must be replaced before a release is
tagged**: look up the commit SHA of the `github/codeql-action` release you trust, put it in place of `PIN_BY_SHA`, and
keep the version in the trailing comment. Until then the action does not resolve and a job that uses it fails at its
setup, which is the intended failure; `tests/test_dist_scripts.py` checks that it is the only unpinned reference.

## Code scanning

The SARIF report uses the finding code as the rule id, so a rule can be filtered by code in the alert list. Upload needs
`security-events: write`; on a private repository it also needs code scanning to be available for the plan.

The upload never fails the job. A pull request from a fork gets a read-only token, so the upload is skipped there (the
log says so in a notice, and the SARIF run is not made). Where the upload is attempted and refused (a read-only token
for another reason, code scanning not available), the log has a warning and the job result still follows the exit code.

## The pull request comment

`scripts/pr_comment.py` summarises the new findings in one comment, errors first, each with a link to the line in the
commit. It keeps a hidden marker and edits that comment on the next push instead of adding another. When there are no
findings and no earlier comment, nothing is posted. It uses `GITHUB_TOKEN` through the REST API and needs
`pull-requests: write`. A token that may not comment (a fork) is a warning and does not fail the check.

Text that comes from a finding is escaped before it goes into the table, so a message cannot add a link, emphasis or an
HTML tag; a `code span` in a message is kept as written. Paths in the links are URL-quoted.

Run it by hand to see the text without calling anything:

```bash
dead-code-checker graph check --scope branch --format json > findings.json
python3 scripts/pr_comment.py --findings findings.json --dry-run
```

The same text goes to the job summary of every run.

## The README badge

The action writes `dead-code-badge.json` in the shields.io endpoint format
(<https://shields.io/badges/endpoint-badge>): `{"schemaVersion": 1, "label": "dead code", "message": "none",
"color": "brightgreen"}`; yellow with a count of warnings, red with a count of errors.

Shields needs the file at a public URL. One way: a job on the default branch commits the file to a `badges` branch (the
README badge points there, and shows as broken until that branch exists), then

```markdown
[![dead code](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FOWNER%2FREPO%2Fbadges%2Fdead-code-badge.json)](https://github.com/OWNER/REPO/actions)
```

Another is a gist that the job updates with a token of your own. Use `scope: repo` on the default branch for a badge
that describes the whole tree; a `branch` scope badge describes only the last change.

## Other CI systems

Nothing here is specific to GitHub except the upload and the comment. Elsewhere, run

```bash
pip install dead-code-checker
dead-code-checker graph check --scope branch --format sarif > dead-code.sarif   # for a SARIF-aware viewer
dead-code-checker graph check --scope branch --format json                      # exit 1 when there is an E
```

and fail the step on exit code 1 (or 2). For a plain commit gate use the pre-commit hook described in the README.

## Coding agents

`dead-code-checker agent install --target claude-code|codex|cursor` prints the snippet that makes an agent run the checker
on the files it edits. It writes nothing unless you pass `--write`, only into the project, and never into your home
directory without `--global --confirm-global`. `--dry-run` shows the result of a write without doing it.

```bash
dead-code-checker agent install --target claude-code                 # print the hook, change nothing
dead-code-checker agent install --target claude-code --write --dry-run
dead-code-checker agent install --target claude-code --write         # .claude/settings.json in the project
```

| target | project file | what it gets |
|---|---|---|
| `claude-code` | `.claude/settings.json` | a `PostToolUse` hook on `Edit`, `Write` and `MultiEdit` that runs `dead-code-checker agent hook`; an `E` finding goes back to the model on standard error (exit 2), warnings stay silent |
| `codex` | `AGENTS.md` | a marked block of instructions |
| `cursor` | `.cursor/rules/dead-code-checker.mdc` | an always-applied rule |

Codex and Cursor get instructions, not a hook; the hard gate stays the pre-commit hook or this action. A second `install`
replaces the marked block instead of adding another, keeps the other settings of an existing file, and refuses a settings
file that is not valid JSON.
