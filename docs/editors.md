# Editors

Two ways to see findings while you edit: a language server (any editor that speaks LSP) and a VS Code extension
that uses the command line directly.

Both read the same thing: the output of `dead-code-checker scan ROOT --json`. Findings describe the files on disk, so
they refresh when a file is saved, not while you type.

## Language server

```bash
dead-code-checker lsp            # JSON-RPC over stdin/stdout; --stdio is accepted and ignored
```

Pure Python standard library, nothing to install beyond the checker itself.

| LSP | behaviour |
|---|---|
| `initialize` | root from `workspaceFolders[0]`, else `rootUri`, else `rootPath`; with none, the git root of the first opened file |
| `initializationOptions` | `{"debounceMs": 400}`, delay before a scan starts (0 to 60000) |
| `textDocument/didOpen` | one scan, the first time only |
| `textDocument/didSave` | a scan of the workspace |
| `textDocument/publishDiagnostics` | one list per file with findings; a file that no longer has any gets an empty list |
| `shutdown`, `exit` | exit code 0 after `shutdown`, 1 without it |

Sync mode is "none": the buffer is never sent (`save.includeText` is false). A request that arrives while a scan is
running stops that scan and starts a new one after the debounce delay. A scan that fails (exit code other than 0 or 1)
is reported with `window/logMessage` and the previous diagnostics stay.

A diagnostic carries:

- `severity`: 1 (error) for level E, 2 (warning) for level W, 3 (information) otherwise;
- `code`: the finding code, for example `deadcode.unreferenced-symbol`;
- `source`: `dead-code-checker`;
- `message`: the finding text, a newline, and one line on what to do about it;
- `range`: the whole finding line, or the first line when the finding is about the file.

The editor snippets below are examples written from each editor's documented LSP configuration; they are not exercised by this repository's tests.

### Neovim

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = { "python", "sh", "javascript", "go", "rust" },
  callback = function(args)
    vim.lsp.start({
      name = "dead-code-checker",
      cmd = { "dead-code-checker", "lsp" },
      root_dir = vim.fs.root(args.buf, { ".git", ".dead-code-checker.toml" }),
    })
  end,
})
```

### Helix

```toml
# ~/.config/helix/languages.toml
[language-server.dead-code-checker]
command = "dead-code-checker"
args = ["lsp"]

[[language]]
name = "python"
language-servers = ["pylsp", "dead-code-checker"]
```

Keep the language's own server in the list; the checker only adds diagnostics.

### Other clients

Any client that can start a command and talk LSP over stdio works: use `dead-code-checker lsp` as the command and
attach it to the languages you check.

## VS Code

The extension lives in `editors/vscode/`. It does not run the language server: it runs the CLI on open and on save,
parses the JSON and fills the Problems panel. That is the simpler and more robust choice for VS Code, because the
extension needs no LSP client (the usual `vscode-languageclient` is an npm package, and the extension has no npm
dependencies and no build step), there is no server process to restart, and it works with any version of the checker
that has `scan --json`. The mapping is the same as in the language server (same severity, code and text).

Settings: `deadCodeChecker.enabled` (default true) and `deadCodeChecker.path` (default `dead-code-checker`).
Commands: `Dead Code Checker: Scan workspace` and `Dead Code Checker: Clear diagnostics`.

### Security: a repository cannot choose what runs

A repository you clone can carry a `.vscode/settings.json`. The extension therefore never lets a workspace decide which
program starts:

- `deadCodeChecker.path` has the scope `machine`, so VS Code ignores it in workspace settings, and the extension also reads
  it only from your user settings or its default (never from a workspace or folder value). Any future setting that names a
  program, an argument, an environment variable or a working directory must be machine-scoped too (a test enforces it).
- The value must be a bare name, which is looked up in the absolute entries of `PATH` only (never the workspace folder), or
  an absolute path. A relative path such as `./tool` is refused.
- The package declares limited support for untrusted workspaces and lists `deadCodeChecker.path` as a restricted
  configuration. In a workspace that is not trusted the extension does not start the checker at all; it shows one
  notification and scans when you grant trust.

The language server has no such setting: `dead-code-checker lsp` always starts the checker's own Python interpreter, the
`initializationOptions` it accepts (`debounceMs`) cannot name a program, and `.dead-code-checker.toml` cannot either (a
`path`, `argv`, `args`, `env`, `cmd`, `command`, `exe` or `binary` key under `[graph]` is a `dc.bad-config` error). The
configuration of your editor's LSP client (the `cmd` in the snippets above) is your own, user-level choice; keep it out of
project-local files you did not write.

Install it from source without publishing anything:

```bash
cd editors/vscode
npx --yes @vscode/vsce package          # needs network; writes dead-code-checker-0.1.0.vsix
code --install-extension dead-code-checker-0.1.0.vsix
```

### Publishing (maintainers)

Needs network and accounts, so it is a manual step:

```bash
cd editors/vscode
npx --yes @vscode/vsce package
npx --yes @vscode/vsce publish -p "$VSCE_PAT"     # Visual Studio Marketplace
npx --yes ovsx publish dead-code-checker-0.1.0.vsix -p "$OVSX_PAT"   # Open VSX
```

The `publisher` field in `editors/vscode/package.json` must be the id of your Marketplace publisher.

## Tests

```bash
python3 -m pytest -q tests/test_lsp.py tests/test_editors.py     # protocol over pipes, manifest checks, node tests
node editors/vscode/test/map.test.js                              # JSON to diagnostics
node editors/vscode/test/extension.test.js                        # the extension against a stub of the vscode API
```
