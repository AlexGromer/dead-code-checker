"use strict";
// Pure mapping from the CLI's `scan --json` output to diagnostic entries. No vscode API here, so plain node can test it.

const path = require("path");
const WHY = require("./why.json");

const SOURCE = "dead-code-checker";

function whyOf(code) {
  return WHY[String(code).replace(/^deadcode\./, "")] || "";
}

function severityOf(level) {
  return level === "E" ? "error" : level === "W" ? "warning" : "information";
}

// Empty output means no findings. A list, or an object with a `findings` list, is accepted; anything else throws.
function parseFindings(text) {
  const t = String(text).trim();
  if (!t) return [];
  let data = JSON.parse(t);
  if (data && !Array.isArray(data) && typeof data === "object") data = data.findings;
  if (!Array.isArray(data)) throw new Error("unexpected JSON shape");
  return data.filter((f) => f && typeof f === "object" && typeof f.path === "string");
}

// [{file, line (0-based), severity, code, message, source}]; a finding whose path leaves `folder` is dropped.
function toEntries(findings, folder) {
  const base = path.resolve(folder);
  const out = [];
  for (const f of findings) {
    const full = path.resolve(base, f.path);
    const rel = path.relative(base, full);
    if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) continue;
    const code = String(f.code === undefined ? "" : f.code);
    const why = whyOf(code);
    const msg = String(f.msg === undefined ? "" : f.msg);
    out.push({
      file: full,
      line: Number.isInteger(f.line) && f.line > 0 ? f.line - 1 : 0,
      severity: severityOf(f.level),
      code,
      message: why ? msg + "\n" + why : msg,
      source: SOURCE,
    });
  }
  return out;
}

module.exports = { SOURCE, whyOf, severityOf, parseFindings, toEntries };
