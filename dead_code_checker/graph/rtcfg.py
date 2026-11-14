"""graph.rtcfg: the `[graph.runtime]` table of a project and the data of the runtime-signal mode (data/runtime.json).

The runtime mode runs the tests (or a configured command) of a project UNDER A TRACER on ONE separate execution target, an ssh host the
user names, inside a bubblewrap box there, and brings back ONLY the set of symbols that ran: a symbol that ran is never reported dead,
a symbol that did not run adds nothing. Nothing in this tool runs project code on the machine it runs on. This module is the light
part (stdlib only, no import of the checker): graph.config reads the table through it, the controlling half and the target half use
its helpers. Nothing here contacts anything.

    [graph.runtime]
    enabled = false          opt-in (default false)
    host = "ci-box"          an ssh destination or an alias of the user's own ssh config (letters, digits . - _ : [ ], no leading -)
    command = "python3 -m pytest -q"     a string (split like a shell WITHOUT a shell: no pipes, no `&&`) or a list of strings; it is DATA for
                             the target and runs there only, in the box. Without it the default of each language of data/runtime.json
                             runs (python: pytest, go: go test)
    timeout = 900            seconds for all runs together (default data timeouts.default_s; never above limits.wall_s)
    refresh_days = 7         a stored run older than this is run again (default data refresh_days)

A host named by the file is used only when the user also allows it: the variable DEAD_CODE_CHECKER_RUNTIME_HOSTS (comma-separated) names it,
or the user passes the host on the command line. The repository's own file never chooses where its code runs on its own, nor which login
or port is used there: the user approves a host, so the login comes only from the user's side (--user / --port, an entry
`user@host:port` of the variable, or the user's own ssh configuration); `user` and `port` in the file are ignored.

    parse(raw) -> (RuntimeCfg, [problem text])    a wrong key or value is a problem (graph.config makes it W dc.bad-config); a bad
                                                  host leaves the mode off, any other wrong value falls back to its default; `user` and
                                                  `port` are ignored with one note (the login is the user's, never the file's)
    host_problem(host) -> str                     "" or why an ssh destination is refused
    hosts_allowed(environ=None) -> set            the hosts of DEAD_CODE_CHECKER_RUNTIME_HOSTS (lower case, valid ones only)
    allowed_logins(environ=None) -> dict          host -> Login(user, port) of those entries: `[user@]host[:port]`, the user's side of the login
    parse_entry(text) -> (user, host, port, why)  one entry of that variable
    host_allowed(host, environ=None) -> bool      whether `host` is in that set
    RuntimeCfg.wall_s() / .refresh()              the effective values
    skip_line(why) -> str                         THE one line of a step that did not run on any target (nothing ran on this machine)
    argv_problem(argv) -> str                     "" or why a command cannot be sent
    first_error(out, err, strip, n) -> str        the line of a check's output that says what broke (secret-looking parts masked)
    mask_secrets(text) -> str                     `text` with the usual secret carriers replaced by ***; best effort
    classes() -> dict                             which checks of a language run the project's code (target only) and which only read it
    data() -> dict                                data/runtime.json (cached)
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Dict, List, Mapping, NamedTuple, Optional, Set, Tuple

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "runtime.json")
KEYS = ("enabled", "host", "user", "port", "command", "timeout", "refresh_days")
ENV_HOSTS = "DEAD_CODE_CHECKER_RUNTIME_HOSTS"
SKIP = "runtime mode: no execution target"
MAX_ARGS, MAX_ARG = 64, 4096
MAX_HOST = 253
_HOST_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_:[]")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
_CACHE: dict = {}


def data() -> dict:
    if "d" not in _CACHE:
        with open(DATA, encoding="utf-8") as fh:
            _CACHE["d"] = json.load(fh)
    return _CACHE["d"]


def skip_line(why: str = "") -> str:
    """The ONE line of a runtime step that did not run: `runtime mode: no execution target (<why>)`."""
    why = " ".join(str(why).split())[: data()["result"]["note_chars"]]
    return f"{SKIP} ({why})" if why else SKIP


def argv_problem(argv) -> str:
    """ "" when `argv` is a command the target may be asked to run (a non-empty list of short strings without NUL or newline)."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) and a for a in argv):
        return "the command must be a non-empty list of non-empty strings"
    if len(argv) > MAX_ARGS or any(len(a) > MAX_ARG for a in argv):
        return f"the command has more than {MAX_ARGS} arguments or an argument longer than {MAX_ARG} characters"
    if any("\0" in a or "\n" in a or "\r" in a for a in argv):
        return "the command has a NUL, a newline or a carriage return in an argument"
    return ""


def host_problem(host) -> str:
    """ "" when `host` may be handed to ssh as a destination, else why not. Strict by design: a value that starts with `-` would be an
    option, whitespace or a control character would split or smuggle one, anything outside the listed characters is not a host name."""
    if not isinstance(host, str) or not host:
        return "the host must be a non-empty text"
    if len(host) > MAX_HOST:
        return f"the host is longer than {MAX_HOST} characters"
    if host[0] == "-":
        return "the host starts with '-' (it would be an ssh option)"
    bad = sorted({c for c in host if c not in _HOST_OK})
    if bad:
        shown = "".join(c if c.isprintable() and not c.isspace() else f"\\x{ord(c):02x}" for c in bad[:4])
        return f"the host has characters outside letters, digits and . - _ : [ ] ({shown})"
    return ""


def user_problem(user) -> str:
    """ "" when `user` looks like a login name."""
    if not isinstance(user, str) or not _USER.fullmatch(user):
        return "the user must be a login name (letters, digits, . - _ ; at most 32; not starting with a digit or -)"
    return ""


class Login(NamedTuple):
    user: str = ""  # "" = the user's ssh configuration decides
    port: int = 0  # 0 = the ssh default or the user's ssh configuration


_ENTRY_PORT = re.compile(r"(\[[^\]]+\]|[^:\[\]]+):([0-9]{1,5})")


def parse_entry(text) -> Tuple[str, str, int, str]:
    """(user, host, port, problem) of one entry of DEAD_CODE_CHECKER_RUNTIME_HOSTS: `[user@]host[:port]` (an IPv6 host with a port is
    `[::1]:22`; a bare IPv6 host has no port). The host is lower case; problem is "" when the entry is usable."""
    t = str(text).strip()
    user, port = "", 0
    if "@" in t:
        user, _, t = t.rpartition("@")
        why = user_problem(user)
        if why:
            return "", "", 0, why
    m = _ENTRY_PORT.fullmatch(t)
    if m:
        t, port = m.group(1), int(m.group(2))
        if not 1 <= port <= 65535:
            return "", "", 0, "the port must be 1..65535"
    host = t.lower()
    why = host_problem(host)
    return (user, host, port, "") if not why else ("", "", 0, why)


def allowed_logins(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Login]:
    """The hosts the user allows a repository's file to name, with the login the user gave for each (the first entry of a host wins):
    DEAD_CODE_CHECKER_RUNTIME_HOSTS, comma-separated entries `[user@]host[:port]`. An entry that is not usable is dropped."""
    env = os.environ if environ is None else environ
    out: Dict[str, Login] = {}
    for raw in (env.get(ENV_HOSTS) or "").split(","):
        if not raw.strip():
            continue
        user, host, port, why = parse_entry(raw)
        if not why:
            out.setdefault(host, Login(user, port))
    return out


def hosts_allowed(environ: Optional[Mapping[str, str]] = None) -> Set[str]:
    """The hosts (lower case) the user allows a repository's file to name; see allowed_logins."""
    return set(allowed_logins(environ))


def host_allowed(host: str, environ: Optional[Mapping[str, str]] = None) -> bool:
    return isinstance(host, str) and host.strip().lower() in hosts_allowed(environ)


MASK = "***"
_SECRET_HINT = re.compile(
    r"(?i)sk-|AIza|gh[pousr]_|github_pat_|AKIA|glpat-|xox[baprs]-|eyJ|bearer|PRIVATE KEY|://|pass|pwd|secret|token|api[_-]?key|access[_-]?key|authoriz|credential"
)
_SECRET_TOKENS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{12,}")
_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)")
_KEY_VALUE = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|authorization|credential)[a-z0-9_.-]*)"
    r"(\s*[:=]\s*)(?!\*\*\*)(\"[^\"]*\"|'[^']*'|[^\s,;'\"}]+)"
)


def mask_secrets(text: str) -> str:
    """`text` with the usual secret carriers replaced by ***. Best effort (a filter for what is shown, not a guarantee): the output of a
    check can quote a config line, an environment dump or a URL with a password, and the line goes to the screen, the JSON output and the stored run."""
    text = str(text)
    if not _SECRET_HINT.search(text):
        return text
    for rx in _SECRET_TOKENS:
        text = rx.sub(MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _USERINFO.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    return _KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)


def first_error(out: str, err: str, strip: str = "", n: int = 300) -> str:
    """The line of a tool's output that says what broke: the first non-empty line that is no header ("# pkg", "FAIL") and carries a
    position or the word error; else the first non-empty one. The directory of the copy (`strip`) is cut off the paths; at most `n` characters.
    """
    lines = [x.strip() for x in (err + "\n" + out).splitlines() if x.strip()]
    if strip:
        lines = [x.replace(strip.rstrip("/") + "/", "") for x in lines]
    good = [x for x in lines if not x.startswith(("#", "FAIL", "ok ", "---", "==="))]
    hit = next((x for x in good if ": " in x or "error" in x.lower()), None) or (good or lines or ["no message"])[0]
    return mask_secrets(hit)[:n]  # masked before the cut: a secret cut in the middle would survive as a prefix


class RuntimeCfg(NamedTuple):
    enabled: bool = False
    host: str = ""  # "" = none in the file (the command line may give one)
    command: Tuple[str, ...] = ()  # () = the default command of each language
    timeout: int = 0  # 0 = the default
    refresh_days: int = 0  # 0 = the default

    def wall_s(self) -> int:
        d = data()
        return min(self.timeout or d["timeouts"]["default_s"], d["limits"]["wall_s"])

    def refresh(self) -> int:
        return self.refresh_days or data()["refresh_days"]


OFF = RuntimeCfg()


def _int(v, lo: int = 1, hi: Optional[int] = None) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def parse(raw) -> Tuple[RuntimeCfg, List[str]]:
    """(config, problems) of a `[graph.runtime]` table. Nothing is run or contacted here. A table that is not a table, an `enabled` that is
    not true/false, or a bad host leaves the mode off; any other wrong value falls back to its default. `user` and `port` are never read:
    the login belongs to the person who runs the tool, not to the repository's file (one note says so)."""
    if not isinstance(raw, dict):
        return OFF, ["[graph.runtime] must be a table: the runtime mode stays off"]
    probs: List[str] = []
    for k in sorted(set(raw) - set(KEYS)):
        probs.append(f"unknown key {k!r} in [graph.runtime] (known: {', '.join(KEYS)})")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        return OFF, probs + ["bad value for 'enabled' in [graph.runtime] (true or false): the runtime mode stays off"]
    fatal = False
    host = ""
    if "host" in raw:
        why = host_problem(raw["host"])
        if why:
            probs.append(f"bad value for 'host' in [graph.runtime]: {why}")
            fatal = True
        else:
            host = raw["host"]
    ignored = [k for k in ("user", "port") if k in raw]
    if ignored:
        probs.append(
            f"{' and '.join(repr(k) for k in ignored)} in [graph.runtime] {'is' if len(ignored) == 1 else 'are'} ignored: the login belongs to "
            f"whoever runs the tool, not to a repository's file; give it with --user / --port, as user@host:port in ${ENV_HOSTS}, or in your ssh configuration"
        )
    if fatal:
        return OFF, probs + ["the runtime mode stays off"]
    command: Tuple[str, ...] = ()
    if "command" in raw:
        c = raw["command"]
        why = ""
        argv = None
        try:
            argv = shlex.split(c) if isinstance(c, str) else list(c) if isinstance(c, list) else None
            why = argv_problem(argv) if argv is not None else "it must be a string or a list of strings"
        except ValueError as e:
            why = f"it cannot be split: {e}"
        if why:
            probs.append(
                f"bad value for 'command' in [graph.runtime]: {why}; the default command of each language is used"
            )
        else:
            command = tuple(argv)
    out = {}
    for k in ("timeout", "refresh_days"):
        if k in raw:
            if _int(raw[k]):
                out[k] = raw[k]
            else:
                probs.append(f"bad value for {k!r} in [graph.runtime] (a positive integer): default used")
    return RuntimeCfg(enabled, host, command, out.get("timeout", 0), out.get("refresh_days", 0)), probs


def classes() -> dict:
    """language -> {"executing": text, "reading": text}: which checks run the project's code (target only) and which only read it."""
    return {k: v for k, v in data()["classes"].items() if not k.startswith("_")}
