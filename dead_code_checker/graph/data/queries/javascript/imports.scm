; graph.extract.ts_generic imports of JavaScript. One match = one fragment of a statement; the mapping groups the fragments by the
; @import / @reexport node. @module = the string, @name (+ @alias) = a specifier, @default = a default binding, @ns = a
; whole-module binding, @star = `*`, @req = the callee of a require-like call (kept only when its text is require, import or URL),
; @pattern = the first argument of such a call when it is no plain string, @await = the await_expression of a declarator's value.

; import "m";  import d from "m";  import * as ns from "m";  import { a, b as c } from "m";
(import_statement source: (string) @module) @import
(import_statement (import_clause (identifier) @default) source: (string) @module) @import
(import_statement (import_clause (namespace_import (identifier) @ns)) source: (string) @module) @import
(import_statement (import_clause (named_imports (import_specifier name: (_) @name alias: (_)? @alias))) source: (string) @module) @import

; export * from "m";  export * as ns from "m";  export { a, b as c } from "m";
(export_statement "*" @star source: (string) @module) @reexport
(export_statement (export_clause (export_specifier name: (_) @name alias: (_)? @alias)) source: (string) @module) @reexport
(export_statement (namespace_export (identifier) @ns) source: (string) @module) @reexport

; require("m"), import("m"), const x = require("m"), const { a, b: c } = require("m")
(call_expression function: (_) @req arguments: (arguments . (string) @module)) @import
(variable_declarator name: (identifier) @ns value: (call_expression function: (_) @req arguments: (arguments . (string) @module))) @import
(variable_declarator name: (object_pattern (shorthand_property_identifier_pattern) @name) value: (call_expression function: (_) @req arguments: (arguments . (string) @module))) @import
(variable_declarator name: (object_pattern (pair_pattern key: (_) @name value: (identifier) @alias)) value: (call_expression function: (_) @req arguments: (arguments . (string) @module))) @import
; const { a, ...rest } = require("m"): the rest element takes every other export, the namespace as a whole
(variable_declarator name: (object_pattern (rest_pattern (identifier) @ns)) value: (call_expression function: (_) @req arguments: (arguments . (string) @module))) @import

; await import("m"): the declarator binds the module value (the call is inside its await_expression, @await); without await the
; declarator holds a promise: `const p = import("m")` binds nothing (the mapping drops its bindings)
(variable_declarator name: (identifier) @ns value: (await_expression (call_expression function: (_) @req arguments: (arguments . (string) @module))) @await) @import
(variable_declarator name: (object_pattern (shorthand_property_identifier_pattern) @name) value: (await_expression (call_expression function: (_) @req arguments: (arguments . (string) @module))) @await) @import
(variable_declarator name: (object_pattern (pair_pattern key: (_) @name value: (identifier) @alias)) value: (await_expression (call_expression function: (_) @req arguments: (arguments . (string) @module))) @await) @import
(variable_declarator name: (object_pattern (rest_pattern (identifier) @ns)) value: (await_expression (call_expression function: (_) @req arguments: (arguments . (string) @module))) @await) @import

; import(`./l/${x}.js`), require("./c/" + name), require(name), require(path.join(__dirname, "h", n)), import.meta.glob(["./a/*.ts"]): the
; first argument is no plain string, @pattern is its text (the mapping turns it into a glob; a plain string is read by the @module patterns)
(call_expression function: (_) @req arguments: (arguments . (_) @pattern)) @import
; a comment before the argument (`import(/* webpackChunkName: "x" */ "./x")`) is a named child too: the argument after it is read the same way
(call_expression function: (_) @req arguments: (arguments (comment) . (string) @module)) @import
(call_expression function: (_) @req arguments: (arguments (comment) . (_) @pattern)) @import

; new URL("./worker.js", import.meta.url): a file the bundler loads
(new_expression constructor: (identifier) @req arguments: (arguments . (string) @module . (member_expression object: (meta_property)))) @import
