; graph.extract.ts_generic imports of Java. Capture vocabulary: @import = the whole import statement; lang_rules.parse_import reads
; its text with the "import" regex of data/frameworks.json (static and wildcard imports included).
(import_declaration) @import
; the package statement is no import (lang_rules.parse_import reads none): it only keeps the names in it from being read as uses
(package_declaration) @import
