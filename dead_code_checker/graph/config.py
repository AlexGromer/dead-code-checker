"""graph.config: the `[graph]` table of `.dead-code-checker.toml` and the DEAD_CODE_CHECKER_* variables.

    mode = "closed_world" | "library"     closed_world: nothing outside the repository calls into it
    enabled = false                       needs disabled_reason (text) and disabled_until (ISO date); past its date the
                                          check runs again (W dc.disabled-expired), a valid disable is W on every run
    viewer_code = true                    false: the pages carry no source code
    viewer_code_bytes = 6291456           the budget of the source text of a page (> 0)
    [graph.budget]  commit_s, branch_s    seconds a level may run (defaults 20 and 900; the file cannot ask for more
                                          than 120 and 3600)
    [[graph.waive]]  code, path_glob | engine, reason, until      a finding code that a path or an engine may keep,
                                          until a date; a waive past its date is E dc.waive-expired
    [graph.runtime]  enabled, host, command, timeout, refresh_days      the runtime-signal mode (graph.rtcfg): the
                                          tests run under a tracer on ONE ssh host the user names, in a box there; a wrong value is
                                          W dc.bad-config and the mode stays off or on its default. `command` here is data for that
                                          host and never runs on this machine; a host named by the file is used only when the user
                                          allows it (DEAD_CODE_CHECKER_RUNTIME_HOSTS or the command line); `user` and `port` are
                                          ignored with one note: the login belongs to whoever runs the tool

The table belongs to the graph checker; the `[deadcode]` table of the text checker is not read here. The file lives in
the analysed repository, which may be somebody else's: it is read only as a regular file of at most 1 MB inside the
repository, and it cannot choose what runs or where data goes. A path, argv, env, engines_dir, cmd, command, exe or
binary key is E dc.bad-config, as is any unknown key; a wrong value is W dc.bad-config and the default stays.

Variables (they override the file; a wrong value is a W and is ignored): DEAD_CODE_CHECKER_MODE, _VIEWER_CODE
(1/0, true/false), _VIEWER_CODE_BYTES, _BUDGET_COMMIT_S, _BUDGET_BRANCH_S (the user's own: not clamped to the file
ceilings). DEAD_CODE_CHECKER_HOME is the data root (graph.paths), DEAD_CODE_CHECKER_JOBS the worker count
(graph.par).

    cfg = load(root); cfg.is_waived("dc.dead", path="a/b.py"); cfg.enabled; cfg.budget("commit"); cfg.as_dict()
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import os
import stat
import tomllib
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from . import rtcfg

FILE = ".dead-code-checker.toml"
TABLE = "graph"
MAX_BYTES = 1_000_000
MODES = ("closed_world", "library")
KEYS = ("enabled", "disabled_reason", "disabled_until", "mode", "budget", "viewer_code", "viewer_code_bytes", "waive", "runtime")
FORBIDDEN = ("path", "paths", "argv", "args", "env", "engines_dir", "cmd", "command", "exe", "binary")
WAIVE_KEYS = ("code", "path_glob", "engine", "reason", "until")
UNWAIVABLE = ("dc.bad-config",)
BUDGETS = {"commit_s": 20.0, "branch_s": 900.0}
CEIL = {"commit_s": 120.0, "branch_s": 3600.0}
LEVELS = {"commit": "commit_s", "branch": "branch_s"}
VIEWER_BYTES = 6 * 1024 * 1024
ENV = "DEAD_CODE_CHECKER_"


class Problem(NamedTuple):
    level: str  # E | W
    where: str  # the config file, or the variable
    code: str
    msg: str

    def render(self) -> str:
        return f"dead-code-checker: {self.level} {self.where}: {self.code}: {self.msg}"


class Waive(NamedTuple):
    code: str
    path_glob: Optional[str]
    engine: Optional[str]
    reason: str
    until: _dt.date


class Disabled(NamedTuple):
    off: bool  # the check is off right now (valid, unexpired)
    reason: Optional[str]
    until: Optional[_dt.date]
    expired: bool


class Config(NamedTuple):
    mode: str
    disabled: Disabled
    budgets: Dict[str, float]
    viewer_code: bool
    viewer_code_bytes: int
    waives: Tuple[Waive, ...]
    problems: Tuple[Problem, ...]
    source: str  # the config file that was read, "" when none
    today: _dt.date
    runtime: rtcfg.RuntimeCfg = rtcfg.OFF

    @property
    def enabled(self) -> bool:
        return not self.disabled.off

    def budget(self, level: str) -> float:
        """Seconds the level (commit | branch) may run."""
        return self.budgets[LEVELS[level]]

    def is_waived(self, code: str, path: Optional[str] = None, engine: Optional[str] = None) -> bool:
        """True when a valid, unexpired waive covers the code for this path (glob) or engine (exact name)."""
        if code in UNWAIVABLE:
            return False
        p = path.replace(os.sep, "/").removeprefix("./") if path is not None else None
        for w in self.waives:
            if w.code != code or w.until < self.today:
                continue
            if w.engine is not None and engine == w.engine:
                return True
            if w.path_glob is not None and p is not None and fnmatch.fnmatchcase(p, w.path_glob):
                return True
        return False

    def as_dict(self) -> dict:
        d, r = self.disabled, self.runtime
        return {
            "config_file": self.source,
            "mode": self.mode,
            "enabled": self.enabled,
            "disabled": {"until": d.until.isoformat() if d.until else None, "reason": d.reason, "expired": d.expired},
            "budget": dict(sorted(self.budgets.items())),
            "viewer_code": self.viewer_code,
            "viewer_code_bytes": self.viewer_code_bytes,
            "waive": [
                {
                    "code": w.code,
                    "path_glob": w.path_glob,
                    "engine": w.engine,
                    "reason": w.reason,
                    "until": w.until.isoformat(),
                }
                for w in self.waives
            ],
            "runtime": {
                "enabled": r.enabled,
                "host": r.host,
                "command": list(r.command),
                "timeout": r.wall_s(),
                "refresh_days": r.refresh(),
            },
            "problems": [p._asdict() for p in self.problems],
        }


def to_date(v: Any) -> Optional[_dt.date]:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        try:
            return _dt.date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _read_table(root: str, problems: List[Problem]) -> Tuple[dict, str]:
    """The [graph] table of the repository's config file ({} when absent or not readable, with a problem)."""
    path = os.path.join(root, FILE)
    try:
        st = os.lstat(path)
    except OSError:
        return {}, ""
    real, top = os.path.realpath(path), os.path.realpath(root)
    if not stat.S_ISREG(st.st_mode) or not (real == top or real.startswith(top + os.sep)):
        problems.append(Problem("W", FILE, "dc.bad-config", "not read: not a regular file inside the repository"))
        return {}, FILE
    if st.st_size > MAX_BYTES:
        problems.append(Problem("W", FILE, "dc.bad-config", f"not read: larger than {MAX_BYTES} bytes"))
        return {}, FILE
    try:
        with open(path, "rb") as fh:
            doc = tomllib.loads(fh.read(MAX_BYTES + 1).decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        problems.append(Problem("E", FILE, "dc.bad-config", f"cannot read: {str(e)[:120]}"))
        return {}, FILE
    tbl = doc.get(TABLE, {})
    if not isinstance(tbl, dict):
        problems.append(Problem("E", FILE, "dc.bad-config", f"[{TABLE}] must be a table"))
        return {}, FILE
    return tbl, FILE


def _waives(raw: Any, where: str, today: _dt.date, problems: List[Problem]) -> List[Waive]:
    def bad(msg: str) -> None:
        problems.append(Problem("E", where, "dc.bad-config", msg))

    out: List[Waive] = []
    if not isinstance(raw, list):
        bad("waive must be an array of tables")
        return out
    for i, it in enumerate(raw):
        tag = f"waive[{i}]"
        if not isinstance(it, dict):
            bad(f"{tag} must be a table")
            continue
        extra = sorted(set(it) - set(WAIVE_KEYS))
        if extra:
            bad(f"{tag}: unknown key {extra[0]!r} (known: {', '.join(WAIVE_KEYS)})")
            continue
        code, glob, eng = it.get("code"), it.get("path_glob"), it.get("engine")
        reason, until = it.get("reason"), to_date(it.get("until"))
        if not (isinstance(code, str) and code):
            bad(f"{tag}: code is mandatory")
        elif code in UNWAIVABLE:
            bad(f"{tag}: {code} cannot be waived")
        elif (glob is None) == (eng is None) or not isinstance(glob if eng is None else eng, str):
            bad(f"{tag}: exactly one of path_glob or engine (text)")
        elif not (isinstance(reason, str) and reason.strip()) or until is None:
            bad(f"{tag}: reason (text) and until (ISO date) are mandatory")
        elif until < today:
            problems.append(Problem("E", where, "dc.waive-expired", f"{tag} {code}: until {until} has passed"))
        else:
            out.append(Waive(code, glob, eng, reason, until))
    return out


def _budgets(raw: Any, where: str, problems: List[Problem]) -> Dict[str, float]:
    """The file's [graph.budget]: a bad value keeps the default, a value above the ceiling is clamped."""
    out = dict(BUDGETS)
    if not isinstance(raw, dict):
        problems.append(Problem("W", where, "dc.bad-budget", f"[{TABLE}.budget] must be a table"))
        return out
    for k, v in sorted(raw.items()):
        if k not in BUDGETS:
            problems.append(
                Problem("W", where, "dc.bad-budget", f"unknown budget key {k!r} (known: {', '.join(BUDGETS)})")
            )
        elif not _num(v):
            problems.append(
                Problem("W", where, "dc.bad-budget", f"budget {k} must be a positive number: {BUDGETS[k]:g} is used")
            )
        else:
            out[k] = min(float(v), CEIL[k])
            if v > CEIL[k]:
                problems.append(
                    Problem(
                        "W",
                        where,
                        "dc.bad-budget",
                        f"budget {k} = {v} is above the ceiling {CEIL[k]:g}: the ceiling is used",
                    )
                )
    return out


def _env(
    environ: Mapping[str, str], problems: List[Problem], mode: str, budgets: Dict[str, float], vcode: bool, vbytes: int
) -> Tuple[str, Dict[str, float], bool, int]:
    """The variables over the file's values: (mode, budgets, viewer_code, viewer_code_bytes)."""

    def get(name: str) -> Optional[str]:
        v = (environ.get(ENV + name) or "").strip()
        return v or None

    def bad(name: str, why: str) -> None:
        problems.append(Problem("W", ENV + name, "dc.bad-config", f"{why}: the value is ignored"))

    v = get("MODE")
    if v is not None:
        if v in MODES:
            mode = v
        else:
            bad("MODE", f"not one of {', '.join(MODES)}")
    v = get("VIEWER_CODE")
    if v is not None:
        if v.lower() in ("1", "true", "yes", "on"):
            vcode = True
        elif v.lower() in ("0", "false", "no", "off"):
            vcode = False
        else:
            bad("VIEWER_CODE", "not 1/0 or true/false")
    v = get("VIEWER_CODE_BYTES")
    if v is not None:
        if v.isdigit() and int(v) > 0:
            vbytes = int(v)
        else:
            bad("VIEWER_CODE_BYTES", "not an integer > 0")
    for key in BUDGETS:
        v = get("BUDGET_" + key.upper())
        if v is not None:
            try:
                n = float(v)
            except ValueError:
                n = 0.0
            if n > 0 and n != float("inf"):
                budgets[key] = n
            else:
                bad("BUDGET_" + key.upper(), "not a number > 0")
    return mode, budgets, vcode, vbytes


def load(
    root: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    use_file: bool = True,
    today: Optional[_dt.date] = None,
) -> Config:
    """The configuration of the repository at `root`: defaults, then the file, then the variables.

    use_file=False reads no file (a tree whose own config is not our policy)."""
    today = today or _dt.date.today()
    problems: List[Problem] = []
    raw, where = _read_table(root, problems) if use_file else ({}, "")
    for k in raw:
        if k in FORBIDDEN:
            problems.append(
                Problem(
                    "E",
                    where,
                    "dc.bad-config",
                    f"{k!r} is not allowed in [{TABLE}]: a repository never chooses what runs or where data goes",
                )
            )
        elif k not in KEYS:
            problems.append(
                Problem("E", where, "dc.bad-config", f"unknown key {k!r} in [{TABLE}] (known: {', '.join(KEYS)})")
            )

    def bad(msg: str) -> None:
        problems.append(Problem("W", where, "dc.bad-config", msg))

    mode = raw.get("mode", "closed_world")
    if mode not in MODES:
        problems.append(Problem("E", where, "dc.bad-config", f"bad mode {mode!r} (one of {', '.join(MODES)})"))
        mode = "closed_world"
    disabled = Disabled(False, None, None, False)
    reason, until = raw.get("disabled_reason"), to_date(raw.get("disabled_until"))
    if raw.get("enabled") is False:
        if not (isinstance(reason, str) and reason.strip()) or until is None:
            problems.append(
                Problem(
                    "E",
                    where,
                    "dc.bad-config",
                    "enabled=false needs disabled_reason (text) and disabled_until (ISO date)",
                )
            )
        elif until < today:
            problems.append(
                Problem(
                    "W",
                    where,
                    "dc.disabled-expired",
                    f"disabled_until {until} has passed: {reason}; the check runs again",
                )
            )
            disabled = Disabled(False, reason, until, True)
        else:
            problems.append(Problem("W", where, "dc.disabled", f"disabled until {until}: {reason}"))
            disabled = Disabled(True, reason, until, False)
    elif "enabled" in raw and raw["enabled"] is not True:
        bad("bad value for 'enabled': the check stays on")
    elif "disabled_reason" in raw or "disabled_until" in raw:
        problems.append(
            Problem("E", where, "dc.bad-config", "disabled_reason / disabled_until are valid only with enabled=false")
        )
    budgets = _budgets(raw["budget"], where, problems) if "budget" in raw else dict(BUDGETS)
    vcode, vbytes = True, VIEWER_BYTES
    if "viewer_code" in raw:
        if isinstance(raw["viewer_code"], bool):
            vcode = raw["viewer_code"]
        else:
            bad("bad value for 'viewer_code': the default is used")
    if "viewer_code_bytes" in raw:
        v = raw["viewer_code_bytes"]
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            vbytes = v
        else:
            bad("bad value for 'viewer_code_bytes': the default is used")
    waives = _waives(raw["waive"], where, today, problems) if "waive" in raw else []
    runtime = rtcfg.OFF
    if "runtime" in raw:
        runtime, rprobs = rtcfg.parse(raw["runtime"])
        problems.extend(Problem("W", where, "dc.bad-config", m) for m in rprobs)
    mode, budgets, vcode, vbytes = _env(
        os.environ if environ is None else environ, problems, mode, budgets, vcode, vbytes
    )
    return Config(mode, disabled, budgets, vcode, vbytes, tuple(waives), tuple(problems), where, today, runtime)
