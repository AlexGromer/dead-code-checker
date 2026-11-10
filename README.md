# dead-code-checker

Dead code in what a commit or a session just added. It looks at the new lines and answers one question: can anything
reach this? A function nobody calls, a file nobody imports, a registered hook that ignores its input and prints a
constant, a config key nobody reads, a CLI flag nobody uses.

Deterministic, no model, no network, standard library only (Python 3.11 or newer).

## Install

```bash
pipx install git+https://github.com/AlexGromer/dead-code-checker
```

## Use

```bash
dead-code-checker staged                 # the index against HEAD (pre-commit); exit 1 on any E, silent on pass
dead-code-checker session PATH...        # files touched in a working session, against HEAD
dead-code-checker scan PATH... [--report FILE] [--foreign] [--lang java,rb] [--public-api MODE] [--jobs N]
dead-code-checker defaults               # print the [deadcode] config keys
```

`scan` treats everything under the paths as new (an audit). `--foreign` is for a tree that is not yours: no git
command runs in it, symlinks and vendor or generated files are never read, nothing is written inside it, and no
finding is an error.

## Findings

Code is `deadcode.<name>`; E blocks (exit 1), W informs.

| code | level | meaning |
|---|---|---|
| unreferenced-symbol | W (E for Python functions with `function_level = "E"`) | a new top-level function, class or constant nothing else mentions |
| unreferenced-public-symbol | W | the same for an exported symbol in `scan` (see `--public-api`) |
| test-only-symbol | W | used only by tests |
| unreferenced-file | E in `hooks/` and `bin/`, W elsewhere | a new code file no import, registry, doc, test or unit file mentions |
| stub-function, todo-stub | W (E when registered) | the body is only `pass`, `...`, `return None`, `raise NotImplementedError`, `todo!()` or a TODO |
| stub-hook | E | a hook registered in an agent `settings.json` that ignores its input and prints a constant |
| config-key-unread, env-unread | W | a new config key or environment variable no code reads |
| cli-flag-unused | W | an argparse or click flag defined and never read |
| bad-config | E | an unknown key or a bad value in `.dead-code-checker.toml` |

`--public-api report|exempt|as-private` (default `report`): an exported symbol nothing in the tree references is
`unreferenced-public-symbol` (W), is not reported at all (`exempt`), or is an ordinary unreferenced symbol (`as-private`).

Languages: Python, shell, JavaScript, Go, Rust by default. Add Java, C/C++, C#, Kotlin, Swift, Scala, Dart, PHP,
Ruby, Lua, Elixir, Haskell, R and CSS/SCSS/Less selectors with `languages` in the config or `--lang`.

## Config

`.dead-code-checker.toml` at the repository root, table `[deadcode]`. `dead-code-checker defaults` prints every key
with its default. Unknown keys are an error, never ignored.

```toml
[deadcode]
function_level = "E"          # a dead Python function blocks the commit
function_level_dirs = ["src"] # ... only under src/
ignore = ["plugin_*"]         # symbol-name globs never reported
languages = ["py", "java"]
```

Suppress one finding: `# deadcode: ignore` on the definition line or the line above. A whole file:
`deadcode: ignore-file` in its first 10 lines.

## pre-commit

```yaml
repos:
  - repo: https://github.com/AlexGromer/dead-code-checker
    rev: v0.1.0
    hooks:
      - id: dead-code-checker
```

## Limits

Names are matched textually. A dynamic use (`getattr` with a built string, a plugin loader) is invisible and needs
the ignore marker. A mention in a string or a trailing comment counts as a use. Methods are checked for TODO stubs
only. In the extra languages only top-level declarations are read, annotation-managed types count as used, and
reflection and framework conventions are invisible. Precision over recall: when in doubt, the symbol counts as used.

## Roadmap

An optional model layer for suspicious function bodies, methods inside classes, framework conventions.

## License

Apache-2.0, see LICENSE.
