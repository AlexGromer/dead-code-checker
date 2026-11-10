# Changelog

## 0.1.0

First release. Deterministic checks only.

- unreferenced-symbol, test-only-symbol, unreferenced-file, stub-function, todo-stub, stub-hook,
  config-key-unread, env-unread, cli-flag-unused.
- Languages: Python, shell, JavaScript, Go, Rust; with `languages` or `scan --foreign` also Java, C/C++, C#, Kotlin,
  Swift, Scala, Dart, PHP, Ruby, Lua, Elixir, Haskell, R and CSS/SCSS/Less selectors.
- `function_level = "E"` makes a dead Python function an error (opt-in).
- `scan --foreign`: hardened audit of a tree that is not yours (no git run in it, no symlinks, no writes, W only);
  `--public-api report|exempt|as-private` decides what an exported, unreferenced symbol is.
- Config: `.dead-code-checker.toml`, table `[deadcode]`.
