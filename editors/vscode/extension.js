"use strict";
// Runs `dead-code-checker scan ROOT --json` on the workspace folder (when it opens and on save) and shows the
// findings as diagnostics. It shells out to the CLI instead of speaking LSP: no protocol client to carry, nothing to
// restart when a server dies, and it works with every CLI version that has `scan --json`.

const vscode = require("vscode");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const { parseFindings, toEntries } = require("./map");

const DEBOUNCE_MS = 400;
const TIMEOUT_MS = 10 * 60 * 1000;

const SEVERITY = {
  error: () => vscode.DiagnosticSeverity.Error,
  warning: () => vscode.DiagnosticSeverity.Warning,
  information: () => vscode.DiagnosticSeverity.Information,
};

// Settings that name a program are machine-scoped in package.json and read here from the user level only
// (the default or the user value), never from the workspace: a cloned repository's .vscode/settings.json must not
// choose what the editor runs.
const DEFAULT_CLI = "dead-code-checker";

function userValue(c, key, fallback) {
  const i = c.inspect(key);
  const v = i && i.globalValue !== undefined ? i.globalValue : i ? i.defaultValue : undefined;
  return v === undefined || v === null || v === "" ? fallback : v;
}

function settings() {
  const c = vscode.workspace.getConfiguration("deadCodeChecker");
  const cli = userValue(c, "path", DEFAULT_CLI);
  return { enabled: c.get("enabled", true), cli: typeof cli === "string" ? cli : DEFAULT_CLI };
}

// The command to start: an absolute path as given, or a bare name looked up in the absolute entries of PATH only
// (never the workspace folder, which the platform may search first). null with a reason when it cannot be trusted.
function resolveCli(cli) {
  if (/[\0\r\n]/.test(cli)) return { error: "the command contains a control character" };
  if (/[\\/]/.test(cli)) {
    return path.isAbsolute(cli) ? { file: cli } : { error: "deadCodeChecker.path must be a name or an absolute path" };
  }
  const exts = process.platform === "win32" ? ["", ...(process.env.PATHEXT || ".EXE;.CMD").split(";").filter(Boolean)] : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir || !path.isAbsolute(dir)) continue;
    for (const ext of exts) {
      const f = path.join(dir, cli + ext);
      try {
        if (fs.statSync(f).isFile()) return { file: f };
      } catch (e) {
        /* not here */
      }
    }
  }
  return { missing: true, file: cli };
}

const trusted = () => vscode.workspace.isTrusted === true;

function activate(context) {
  const collection = vscode.languages.createDiagnosticCollection("dead-code-checker");
  const out = vscode.window.createOutputChannel("Dead Code Checker");
  const runs = new Map(); // folder path -> { timer, child, uris: Set<string> }
  let warnedMissing = false;
  let warnedUntrusted = false;

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

  // In an untrusted workspace nothing is started: the extension only says so, once.
  function allowed() {
    if (trusted()) return true;
    if (!warnedUntrusted) {
      warnedUntrusted = true;
      vscode.window.showWarningMessage(
        "Dead Code Checker is paused: this workspace is not trusted, so the checker is not started. Trust the workspace to scan it."
      );
    }
    return false;
  }

  function scan(folder) {
    const st = state(folder);
    cancel(st);
    const { enabled, cli: name } = settings();
    if (!enabled) return clearFolder(folder);
    if (!allowed()) return clearFolder(folder);
    const r = resolveCli(name);
    if (r.error) {
      out.appendLine(`not started: ${r.error}`);
      return;
    }
    const cli = r.file;
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
    if (!settings().enabled || !allowed()) return clearFolder(folder);
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
    ...(vscode.workspace.onDidGrantWorkspaceTrust
      ? [vscode.workspace.onDidGrantWorkspaceTrust(() => folders().forEach((f) => schedule(f, 0)))]
      : []),
    vscode.commands.registerCommand("deadCodeChecker.run", () => folders().forEach((f) => scan(f))),
    vscode.commands.registerCommand("deadCodeChecker.clear", () => [...runs.keys()].forEach(clearFolder)),
    { dispose: () => runs.forEach(cancel) }
  );

  folders().forEach((f) => schedule(f, 0));
}

function deactivate() {}

module.exports = { activate, deactivate };
