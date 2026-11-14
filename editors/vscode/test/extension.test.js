"use strict";
// The extension against a stub of the vscode API and a fake CLI. Run with plain node:
//   node editors/vscode/test/extension.test.js
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const posix = process.platform !== "win32";

// cfg holds the user-level values, ws the workspace-level ones, trusted the workspace trust state
function makeVscode(cfg, folder, ws = {}, trusted = true) {
  const handlers = { save: [], config: [], folders: [], trust: [] };
  const defaults = { path: "dead-code-checker" };
  const warnings = [];
  const commands = {};
  const diag = new Map();
  const v = {
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2 },
    Range: class { constructor(a, b, c, d) { this.start = { line: a, character: b }; this.end = { line: c, character: d }; } },
    Diagnostic: class { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } },
    Uri: {
      file: (p) => ({ fsPath: p, scheme: "file", toString: () => "file://" + p }),
      parse: (s) => ({ toString: () => s }),
    },
    languages: {
      createDiagnosticCollection: () => ({
        set: (uri, list) => diag.set(uri.toString(), list),
        delete: (uri) => diag.delete(uri.toString()),
        dispose() {},
      }),
    },
    window: {
      createOutputChannel: () => ({ lines: [], appendLine(l) { this.lines.push(l); }, dispose() {} }),
      showWarningMessage: (m) => warnings.push(m),
    },
    commands: { registerCommand: (n, fn) => ((commands[n] = fn), { dispose() {} }) },
    workspace: {
      workspaceFolders: [{ uri: { fsPath: folder } }],
      isTrusted: trusted,
      getConfiguration: () => ({
        // get() merges the workspace over the user level, as VS Code does; inspect() keeps the levels apart
        get: (k, d) => (k in ws ? ws[k] : k in cfg ? cfg[k] : d),
        inspect: (k) => ({ key: k, defaultValue: defaults[k], globalValue: cfg[k], workspaceValue: ws[k], workspaceFolderValue: ws[k] }),
      }),
      onDidGrantWorkspaceTrust: (cb) => (handlers.trust.push(cb), { dispose() {} }),
      getWorkspaceFolder: (uri) => (uri.fsPath.startsWith(folder) ? { uri: { fsPath: folder } } : undefined),
      onDidSaveTextDocument: (cb) => (handlers.save.push(cb), { dispose() {} }),
      onDidChangeConfiguration: (cb) => (handlers.config.push(cb), { dispose() {} }),
      onDidChangeWorkspaceFolders: (cb) => (handlers.folders.push(cb), { dispose() {} }),
    },
  };
  return { v, handlers, warnings, commands, diag, grant: () => ((v.workspace.isTrusted = true), handlers.trust.forEach((cb) => cb())) };
}

// load a fresh copy of extension.js whose require("vscode") is `stub`
function load(stub) {
  const orig = Module._load;
  Module._load = function (request, ...rest) {
    return request === "vscode" ? stub : orig.call(this, request, ...rest);
  };
  try {
    const p = require.resolve("../extension");
    delete require.cache[p];
    return require(p);
  } finally {
    Module._load = orig;
  }
}

async function until(pred, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (pred()) return;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error("timed out");
}

// a fake CLI: prints what <folder>/.mode says
function fakeCli(dir) {
  const f = path.join(dir, "fake-cli.js");
  fs.writeFileSync(
    f,
    `#!${process.execPath}
const fs = require("fs"), path = require("path");
const folder = process.argv[3];
const mode = fs.readFileSync(path.join(folder, ".mode"), "utf8").trim();
fs.appendFileSync(path.join(folder, ".calls"), process.argv.slice(2).join(" ") + "\\n");
if (mode === "slow") { setTimeout(() => {}, 60000); }
else if (mode === "fail") { console.error("boom"); process.exit(2); }
else if (mode === "empty") { process.exit(0); }
else { console.log(JSON.stringify([
  { level: "E", path: "lib/a.py", line: 2, code: "deadcode.stub-hook", msg: "hook is constant" },
  { level: "W", path: "lib/a.py", line: 1, code: "deadcode.unreferenced-symbol", msg: "f unused" },
  { level: "W", path: "lib/b.py", line: 1, code: "deadcode.unreferenced-file", msg: "b unused" }]));
  process.exit(1); }
`
  );
  fs.chmodSync(f, 0o755);
  return f;
}

function setup(t, { ws = {}, trusted = true } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dcc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const folder = path.join(dir, "ws");
  fs.mkdirSync(folder);
  fs.writeFileSync(path.join(folder, ".mode"), "findings");
  const cfg = { enabled: true, path: fakeCli(dir) };
  const stub = makeVscode(cfg, folder, ws, trusted);
  const ext = load(stub.v);
  const ctx = { subscriptions: [] };
  t.after(() => ctx.subscriptions.forEach((s) => s.dispose && s.dispose()));
  return { dir, folder, cfg, stub, ext, ctx, mode: (m) => fs.writeFileSync(path.join(folder, ".mode"), m) };
}

const calls = (folder) => (fs.existsSync(path.join(folder, ".calls")) ? fs.readFileSync(path.join(folder, ".calls"), "utf8").trim().split("\n") : []);
const uri = (p) => "file://" + p;

test("activation scans the folder and publishes diagnostics per file", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  const a = s.stub.diag.get(uri(path.join(s.folder, "lib", "a.py")));
  assert.strictEqual(a.length, 2);
  assert.deepStrictEqual(a.map((d) => d.severity), [0, 1]);
  assert.strictEqual(a[0].code, "deadcode.stub-hook");
  assert.strictEqual(a[0].source, "dead-code-checker");
  assert.strictEqual(a[0].range.start.line, 1);
  assert.ok(a[0].message.startsWith("hook is constant\n"));
  assert.deepStrictEqual(calls(s.folder), [`scan ${s.folder} --root ${s.folder} --json`]);
});

test("a save rescans (debounced) and clears files that no longer have findings", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  s.mode("empty");
  const doc = { uri: { scheme: "file", fsPath: path.join(s.folder, "lib", "a.py") } };
  for (let i = 0; i < 4; i++) s.stub.handlers.save.forEach((cb) => cb(doc));
  await until(() => s.stub.diag.size === 0);
  assert.strictEqual(calls(s.folder).length, 2); // the four saves made one run
});

test("a save outside the workspace or of a non-file document does nothing", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  s.stub.handlers.save.forEach((cb) => cb({ uri: { scheme: "untitled", fsPath: "x" } }));
  s.stub.handlers.save.forEach((cb) => cb({ uri: { scheme: "file", fsPath: "/elsewhere/x.py" } }));
  await new Promise((r) => setTimeout(r, 900));
  assert.strictEqual(calls(s.folder).length, 1);
});

test("disabled: nothing runs and old diagnostics are cleared", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  s.cfg.enabled = false;
  s.stub.handlers.config.forEach((cb) => cb({ affectsConfiguration: () => true }));
  await until(() => s.stub.diag.size === 0);
  assert.strictEqual(calls(s.folder).length, 1);
});

test("a failing scan keeps the old diagnostics and is logged, not shown", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  s.mode("fail");
  await s.stub.commands["deadCodeChecker.run"]();
  await until(() => calls(s.folder).length === 2);
  await new Promise((r) => setTimeout(r, 300));
  assert.strictEqual(s.stub.diag.size, 2);
  assert.deepStrictEqual(s.stub.warnings, []);
});

test("a run in flight is killed by a newer one", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.mode("slow");
  s.ext.activate(s.ctx);
  await until(() => calls(s.folder).length === 1);
  s.mode("empty");
  await s.stub.commands["deadCodeChecker.run"](); // cancels the slow child, starts a new one
  await until(() => calls(s.folder).length === 2);
  await new Promise((r) => setTimeout(r, 300));
  assert.strictEqual(s.stub.diag.size, 0);
});

test("clear command removes diagnostics", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  s.stub.commands["deadCodeChecker.clear"]();
  assert.strictEqual(s.stub.diag.size, 0);
});

test("a missing CLI warns once", async (t) => {
  const s = setup(t);
  s.cfg.path = path.join(s.dir, "no-such-cli");
  s.ext.activate(s.ctx);
  await until(() => s.stub.warnings.length === 1);
  s.stub.commands["deadCodeChecker.run"]();
  await new Promise((r) => setTimeout(r, 400));
  assert.strictEqual(s.stub.warnings.length, 1);
  assert.ok(s.stub.warnings[0].includes("deadCodeChecker.path"));
});

// a program that records that it ran: what a hostile workspace setting would point at
function plant(dir) {
  const marker = path.join(dir, "planted-ran");
  const f = path.join(dir, "planted.js");
  fs.writeFileSync(f, `#!${process.execPath}\nrequire("fs").writeFileSync(${JSON.stringify(marker)}, "ran");\nconsole.log("[]");\n`);
  fs.chmodSync(f, 0o755);
  return { f, ran: () => fs.existsSync(marker) };
}

test("a workspace value of deadCodeChecker.path is never run", { skip: !posix }, async (t) => {
  const dir0 = fs.mkdtempSync(path.join(os.tmpdir(), "dcc-plant-"));
  t.after(() => fs.rmSync(dir0, { recursive: true, force: true }));
  const bad = plant(dir0);
  const s = setup(t, { ws: { path: bad.f } });
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2); // the user's CLI ran, the workspace's did not
  await new Promise((r) => setTimeout(r, 300));
  assert.strictEqual(bad.ran(), false);
  assert.strictEqual(calls(s.folder).length, 1);
});

test("an untrusted workspace starts nothing, says so once, and scans after trust is granted", { skip: !posix }, async (t) => {
  const s = setup(t, { trusted: false });
  s.ext.activate(s.ctx);
  await new Promise((r) => setTimeout(r, 500));
  await s.stub.commands["deadCodeChecker.run"]();
  s.stub.handlers.save.forEach((cb) => cb({ uri: { scheme: "file", fsPath: path.join(s.folder, "lib", "a.py") } }));
  await new Promise((r) => setTimeout(r, 900));
  assert.deepStrictEqual(calls(s.folder), []);
  assert.strictEqual(s.stub.diag.size, 0);
  assert.strictEqual(s.stub.warnings.length, 1);
  assert.ok(/not trusted/.test(s.stub.warnings[0]));
  s.stub.grant();
  await until(() => s.stub.diag.size === 2);
  assert.strictEqual(calls(s.folder).length, 1);
});

test("a relative path setting is refused, not run from the workspace", { skip: !posix }, async (t) => {
  const s = setup(t);
  s.cfg.path = "./fake-cli.js";
  fs.copyFileSync(path.join(s.dir, "fake-cli.js"), path.join(s.folder, "fake-cli.js"));
  fs.chmodSync(path.join(s.folder, "fake-cli.js"), 0o755);
  s.ext.activate(s.ctx);
  await new Promise((r) => setTimeout(r, 600));
  assert.deepStrictEqual(calls(s.folder), []);
});

test("a bare name is looked up in the absolute entries of PATH only", { skip: !posix }, async (t) => {
  const s = setup(t);
  fs.copyFileSync(path.join(s.dir, "fake-cli.js"), path.join(s.dir, "dcc-bare"));
  fs.chmodSync(path.join(s.dir, "dcc-bare"), 0o755);
  const planted = plant(s.folder); // a program with the same name where a relative PATH entry would find it
  fs.renameSync(planted.f, path.join(s.folder, "dcc-bare"));
  const oldPath = process.env.PATH;
  const oldCwd = process.cwd();
  t.after(() => {
    process.env.PATH = oldPath;
    process.chdir(oldCwd);
  });
  process.chdir(s.folder);
  process.env.PATH = [".", "", s.dir].join(path.delimiter);
  s.cfg.path = "dcc-bare";
  s.ext.activate(s.ctx);
  await until(() => s.stub.diag.size === 2);
  assert.strictEqual(planted.ran(), false);
});
