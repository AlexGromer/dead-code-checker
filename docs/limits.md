# Limits

- Matching is textual: `git grep -w -F` over the repository (or a stdlib walk outside git). A name built at run time
  is not found.
- A line that is only a comment is not a use; a trailing comment or a string is.
- Python is parsed (ast); the other languages are read with bounded regular expressions and brace or `end` matching.
  A file that does not parse is reported as `deadcode.unparsed` and not checked.
- YAML is read by regular expression.
- `scan --foreign` assumes the tree is hostile: it reads regular files inside the tree only.
