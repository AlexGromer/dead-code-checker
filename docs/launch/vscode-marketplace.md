# VS Code Marketplace and Open VSX (draft, not submitted)

The extension lives on branch `wf/editors` (`editors/vscode`), which is not merged into the release branch yet. It runs
the command line (`dead-code-checker scan ROOT --json`) on open and on save and shows the findings in the Problems panel.
Do not publish before that branch is merged and the version in `editors/vscode/package.json` is final.

## Listing text

- **Display name:** dead-code-checker
- **Short description:** Dead code findings from the dead-code-checker command-line tool, shown as diagnostics on save.
- **Categories:** Linters. **Keywords:** dead-code, lint, unused.
- **Long description (Marketplace page, from the extension README):**

  > Shows what dead-code-checker finds in your workspace as diagnostics: an unreferenced function, a file nothing
  > imports, a stub, a config key nobody reads. Errors and warnings appear in the Problems panel and as squiggles on the
  > line, with the finding code and one line on what to do. The workspace is scanned when it opens and on every save.
  >
  > The extension does not bundle the checker. Install it first: `pipx install dead-code-checker`, or set
  > `deadCodeChecker.path`. Nothing leaves your machine.

- **Screenshots to produce:** the Problems panel with two findings; an inline diagnostic with its message; the
  settings page.

## VS Code Marketplace

- [ ] A publisher exists under the id in `package.json` (`alexgromer`); create it in the Marketplace management page if it
      does not.
- [ ] A personal access token from the Azure DevOps organization of that publisher, scope "Marketplace: Manage", stored as
      a secret; never committed.
- [ ] `npx @vscode/vsce package` produces the `.vsix`; install it in a clean profile and run the commands "Dead Code
      Checker: Scan workspace" and "Clear diagnostics".
- [ ] `npx @vscode/vsce publish` (or upload the `.vsix` on the management page).
- [ ] `repository`, `license`, `icon` (128 px PNG, to produce) and a changelog entry are present; `.vscodeignore` keeps
      tests out of the package.

## Open VSX (VSCodium, Gitpod and other editors)

- [ ] An account on open-vsx.org, an access token, and the namespace claimed (`ovsx create-namespace <publisher>`, then
      verify the namespace as the site describes).
- [ ] `npx ovsx publish <file>.vsix -p <token>`.
- [ ] The same `.vsix` is used for both registries, so the two listings carry the same version.

## Verify when submitting

The tool names (`vsce`, `ovsx`), the token scope and the namespace verification steps change over time; check the
current documentation of both registries on the day, and correct this list.
