"use strict";
// Run with plain node: node editors/vscode/test/map.test.js
const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const { parseFindings, toEntries, severityOf, whyOf } = require("../map");

const one = { level: "W", path: "lib/a.py", line: 3, code: "deadcode.unreferenced-symbol", msg: "new function f: nothing references it" };

test("parseFindings: empty output means no findings", () => {
  assert.deepStrictEqual(parseFindings(""), []);
  assert.deepStrictEqual(parseFindings("  \n"), []);
});

test("parseFindings: a list, an object with findings, junk entries dropped", () => {
  assert.deepStrictEqual(parseFindings(JSON.stringify([one])), [one]);
  assert.deepStrictEqual(parseFindings(JSON.stringify({ findings: [one] })), [one]);
  assert.deepStrictEqual(parseFindings(JSON.stringify([one, "junk", { nopath: 1 }, null])), [one]);
});

test("parseFindings: bad JSON and wrong shapes throw", () => {
  assert.throws(() => parseFindings("not json"));
  assert.throws(() => parseFindings("3"));
  assert.throws(() => parseFindings('{"findings": 3}'));
});

test("severityOf: E error, W warning, anything else information", () => {
  assert.strictEqual(severityOf("E"), "error");
  assert.strictEqual(severityOf("W"), "warning");
  assert.strictEqual(severityOf("I"), "information");
  assert.strictEqual(severityOf(undefined), "information");
});

test("toEntries: line is 0-based, code kept, message carries the why", () => {
  const folder = path.resolve("/tmp/repo");
  const [e] = toEntries([one], folder);
  assert.strictEqual(e.file, path.join(folder, "lib", "a.py"));
  assert.strictEqual(e.line, 2);
  assert.strictEqual(e.severity, "warning");
  assert.strictEqual(e.code, "deadcode.unreferenced-symbol");
  assert.strictEqual(e.source, "dead-code-checker");
  assert.ok(e.message.startsWith(one.msg + "\n") && e.message.includes("deadcode: ignore"));
});

test("toEntries: a file-level finding (line 0 or missing) lands on the first line; unknown code has no why", () => {
  const entries = toEntries(
    [
      { level: "E", path: "a.py", line: 0, code: "deadcode.future", msg: "m" },
      { level: "W", path: "a.py", code: "deadcode.future", msg: "n" },
    ],
    "/tmp/repo"
  );
  assert.deepStrictEqual(entries.map((e) => e.line), [0, 0]);
  assert.deepStrictEqual(entries.map((e) => e.message), ["m", "n"]);
  assert.strictEqual(entries[0].severity, "error");
});

test("toEntries: paths that leave the folder are dropped", () => {
  const bad = [
    { level: "W", path: "../escape.py", line: 1, code: "c", msg: "m" },
    { level: "W", path: path.resolve("/etc/passwd"), line: 1, code: "c", msg: "m" },
    { level: "W", path: ".", line: 1, code: "c", msg: "m" },
  ];
  assert.deepStrictEqual(toEntries(bad, "/tmp/repo"), []);
});

test("whyOf: known codes with or without the deadcode. prefix", () => {
  assert.ok(whyOf("deadcode.stub-hook"));
  assert.strictEqual(whyOf("stub-hook"), whyOf("deadcode.stub-hook"));
  assert.strictEqual(whyOf("deadcode.nope"), "");
});
