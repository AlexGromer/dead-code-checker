"use strict";
// Runs `dead-code-checker scan ROOT --json` on the workspace folder (when it opens and on save) and shows the
// findings as diagnostics. It shells out to the CLI instead of speaking LSP: no protocol client to carry, nothing to
// restart when a server dies, and it works with every CLI version that has `scan --json`.

const vscode = require("vscode");
const { spawn } = require("child_process");
const { parseFindings, toEntries } = require("./map");

const DEBOUNCE_MS = 400;
const TIMEOUT_MS = 10 * 60 * 1000;

const SEVERITY = {
  error: () => vscode.DiagnosticSeverity.Error,
  warning: () => vscode.DiagnosticSeverity.Warning,
  information: () => vscode.DiagnosticSeverity.Information,
};

function settings() {
  const c = vscode.workspace.getConfiguration("deadCodeChecker");
  return { enabled: c.get("enabled", true), cli: c.get("path", "dead-code-checker") || "dead-code-checker" };
}

function activate(context) {
  const collection = vscode.languages.createDiagnosticCollection("dead-code-checker");
  const out = vscode.window.createOutputChannel("Dead Code Checker");
  const runs = new Map(); // folder path -> { timer, child, uris: Set<string> }
  let warnedMissing = false;

  const state = (folder) => {
    if (!runs.has(folder)) runs.set(folder, { timer: null, child: null, uris: new Set() });
    return runs.get(folder);
  };

  function cancel(st) {
    if (st.timer) clearTimeout(st.timer);
    st.timer = null;
    if (st.child) {
      const c = st.child;
      st.child = null; // its output is stale from here on
      c.kill();
    }
  }

  function show(st, entries) {
    const byUri = new Map();
    for (const e of entries) {
      const uri = vscode.Uri.file(e.file);
      const d = new vscode.Diagnostic(
        new vscode.Range(e.line, 0, e.line, Number.MAX_SAFE_INTEGER),
        e.message,
        SEVERITY[e.severity]()
      );
      d.code = e.code;
      d.source = e.source;
      const k = uri.toString();
      if (!byUri.has(k)) byUri.set(k, { uri, list: [] });
      byUri.get(k).list.push(d);
    }
    for (const k of st.uris) if (!byUri.has(k)) collection.delete(vscode.Uri.parse(k));
    st.uris = new Set(byUri.keys());
    for (const { uri, list } of byUri.values()) collection.set(uri, list);
  }

  function clearFolder(folder) {
    const st = state(folder);
    cancel(st);
    for (const k of st.uris) collection.delete(vscode.Uri.parse(k));
    st.uris = new Set();
  }

  function scan(folder) {
    const st = state(folder);
    cancel(st);
    const { enabled, cli } = settings();
    if (!enabled) return clearFolder(folder);
    const child = spawn(cli, ["scan", folder, "--root", folder, "--json"], { cwd: folder, windowsHide: true });
    st.child = child;
    const bufs = [];
    const errs = [];
    const timeout = setTimeout(() => child.kill(), TIMEOUT_MS);
    child.stdout.on("data", (b) => bufs.push(b));
    child.stderr.on("data", (b) => errs.push(b));
    child.on("error", (e) => {
      clearTimeout(timeout);
      if (st.child !== child) return;
      st.child = null;
      out.appendLine(`cannot run ${cli}: ${e.message}`);
      if (e.code === "ENOENT" && !warnedMissing) {
        warnedMissing = true;
        vscode.window.showWarningMessage(
          `Dead Code Checker: "${cli}" was not found. Install it (pipx install git+https://github.com/AlexGromer/dead-code-checker) or set deadCodeChecker.path.`
        );
      }
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (st.child !== child) return; // cancelled or superseded
      st.child = null;
      if (code !== 0 && code !== 1) {
        out.appendLine(`scan of ${folder} failed (exit ${code}): ${Buffer.concat(errs).toString().slice(-500)}`);
        return;
      }
      try {
        show(st, toEntries(parseFindings(Buffer.concat(bufs).toString()), folder));
      } catch (e) {
        out.appendLine(`scan of ${folder}: ${e.message}`);
      }
    });
  }

  function schedule(folder, delay = DEBOUNCE_MS) {
    const st = state(folder);
    cancel(st);
    if (!settings().enabled) return clearFolder(folder);
    st.timer = setTimeout(() => {
      st.timer = null;
      scan(folder);
    }, delay);
  }

  const folders = () => (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);

  context.subscriptions.push(
    collection,
    out,
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (doc.uri.scheme !== "file") return;
      const f = vscode.workspace.getWorkspaceFolder(doc.uri);
      if (f) schedule(f.uri.fsPath);
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("deadCodeChecker")) folders().forEach((f) => schedule(f, 0));
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => folders().forEach((f) => schedule(f, 0))),
    vscode.commands.registerCommand("deadCodeChecker.run", () => folders().forEach((f) => scan(f))),
    vscode.commands.registerCommand("deadCodeChecker.clear", () => [...runs.keys()].forEach(clearFolder)),
    { dispose: () => runs.forEach(cancel) }
  );

  folders().forEach((f) => schedule(f, 0));
}

function deactivate() {}

module.exports = { activate, deactivate };
