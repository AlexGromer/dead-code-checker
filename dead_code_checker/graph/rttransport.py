"""graph.rttransport: the ssh channel of the runtime-signal mode and the checks that keep this machine and its secrets out of it.

The controlling half (graph.runtime) talks to ONE execution target, an ssh host the user names. Everything that touches the target or
decides whether a host may be used lives here, so it can be read in one place.

    work_machine(host) -> str          "" when `host` is a different machine, else why it is THIS one (loopback, unspecified, this machine's
                                       host name, FQDN or any address of its interfaces, a name /etc/hosts maps to one of those)
    Target(host, user, port)            the destination; .label is `user@host` or `host`
    ssh_argv(target, remote, ...)       the ssh command line: BatchMode, StrictHostKeyChecking=yes (a host key that is not in the user's
                                        known_hosts is never accepted), no forwarding of any kind, no local command, no multiplexing
    put_command / run_command / pull_command / cleanup_command
                                        the four remote commands, ONE fixed form each, built from validated parts only (32 hex digits, 64
                                        hex digits, fixed names): a private 0700 directory below /tmp per run, `env -i` with a small allowlist,
                                        a wrapper that ends the process group when the channel closes, exactly two files pulled, the directory
                                        removed after every started run
    SshTransport(target)                put / run / pull / cleanup / reachable over the system `ssh`; transfers are tar over the same channel
                                        (no scp, rsync or sftp); nothing here raises into the caller: a failure is a (code, reason) pair
    secret_name_hits(paths)             tracked names that look like credentials (the project copy never carries one)
    scan_content(data) -> set           the secret rules that match the bytes (and what is inside zip, tar, gzip, bzip2, xz members)

The public tool has no local execution path: nothing in this module starts the project's code, a test framework or a shell on this machine;
the only process it starts is `ssh`.
"""

from __future__ import annotations

import bz2
import fnmatch
import gzip
import io
import ipaddress
import lzma
import os
import re
import shlex
import signal
import socket
import subprocess
import tarfile
import threading
import zipfile
import zlib
from typing import List, Mapping, NamedTuple, Optional, Tuple

from .. import safepath
from . import rtcfg

# ---------------------------------------------------------------- this machine
LOCAL_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")


def _ip(text: str):
    """The ipaddress object of a literal (a zone id, brackets and an IPv4-mapped IPv6 form are reduced), else None."""
    try:
        a = ipaddress.ip_address(text.strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    return a.ipv4_mapped if getattr(a, "ipv4_mapped", None) else a


def _own_addresses() -> set:
    """The addresses of this machine, read from the system without a packet: what its own names resolve to (getaddrinfo of the host name
    and the FQDN) and, on Linux, every interface address (/proc/net/fib_trie, /proc/net/if_inet6)."""
    out = set()
    for n in {socket.gethostname(), socket.getfqdn()}:
        try:
            for info in socket.getaddrinfo(n, None):
                a = _ip(str(info[4][0]))
                if a is not None:
                    out.add(a)
        except (OSError, UnicodeError):
            pass
    try:
        prev = ""
        with open("/proc/net/fib_trie", encoding="ascii", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("|--"):
                    prev = ln[3:].strip()
                elif ln.startswith("/32 host LOCAL") and (a := _ip(prev)) is not None:
                    out.add(a)
    except OSError:
        pass
    try:
        with open("/proc/net/if_inet6", encoding="ascii", errors="replace") as fh:
            for ln in fh:
                f = ln.split()
                if f and len(f[0]) == 32:
                    out.add(ipaddress.IPv6Address(int(f[0], 16)))
    except (OSError, ValueError):
        pass
    return out


def _hosts_file_addresses(name: str) -> set:
    """The addresses that /etc/hosts gives `name` (no DNS: the user's own file only)."""
    out = set()
    try:
        with open("/etc/hosts", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                f = ln.split("#", 1)[0].split()
                if len(f) > 1 and name in (x.lower() for x in f[1:]) and (a := _ip(f[0])) is not None:
                    out.add(a)
    except OSError:
        pass
    return out


def work_machine(host: str) -> str:
    """ "" when `host` (the ssh host of the target) is a different machine, else why it is THIS one: localhost, a loopback or the
    unspecified address, this machine's own host name or FQDN, any address of its interfaces, or a name that /etc/hosts maps to one of
    those. Code runs only on a SEPARATE machine, never here. Nothing is contacted and no name is looked up in the DNS (a name that only
    the DNS, or the user's ssh config, maps to this machine is the user's configuration to get right).
    """
    h = str(host or "").strip().strip("[]").rstrip(".").lower()
    if "@" in h:
        h = h.rsplit("@", 1)[1]
    if not h:
        return ""
    if h in LOCAL_NAMES or h.endswith(".localhost"):
        return f"{h} is the loopback of this machine"
    own_names = set()
    for n in (socket.gethostname(), socket.getfqdn(), os.uname().nodename if hasattr(os, "uname") else ""):
        n = str(n or "").strip().rstrip(".").lower()
        if n:
            own_names.add(n)
            if "." not in h:  # an unqualified name is the short name of this machine too
                own_names.add(n.split(".", 1)[0])
    if h in own_names:
        return f"{h} is the name of this machine"
    a = _ip(h)
    cands = {a} if a is not None else _hosts_file_addresses(h)
    if not cands:
        return ""
    if any(c.is_loopback or c.is_unspecified for c in cands):
        return f"{h} is a loopback or unspecified address of this machine"
    own = _own_addresses()
    if any(c in own for c in cands):
        return f"{h} is an address of this machine"
    return ""


# ---------------------------------------------------------------- the remote command lines
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DELIVERY = ("manifest.json", "runtime.json")  # the only two files that come back
TOOL_NAMES = (
    "rtbox.py",
    "runtime.json",
    "tracers/py/sitecustomize.py",
)  # the tool's own half that runs on the target (graph.rtbox.delivery_files)
PUT_MEMBERS = ("in.tar",) + tuple(
    "box/" + n for n in TOOL_NAMES
)  # the only members the put command takes out of what it receives
WRAPPER_GRACE = 5
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
# the private base directory of the target user: made 0700 or refused (a pre-created directory of another user or a link is not used)
_BASE = "B=/tmp/.dcc-rt-$(id -u); "
_REFUSE = "echo 'dead-code-checker: '\"$B\"' is not a private directory of this user' >&2; exit 111"
GUARD = (
    _BASE
    + '{ mkdir -p -m 700 "$B" 2>/dev/null; [ -d "$B" ] && [ ! -L "$B" ] && [ -O "$B" ] && chmod 700 "$B" || { '
    + _REFUSE
    + "; }; }"
)
GUARD_RO = _BASE + '{ [ -d "$B" ] && [ ! -L "$B" ] && [ -O "$B" ] || { ' + _REFUSE + "; }; }"

# Runs ON THE TARGET (python3 -I -c). Argument: the grace seconds, then the command. Starts the command in a process group of its own and ends
# that group when its stdin reaches EOF (the ssh channel is gone: a timeout here, a network drop; sshd closes it) or on TERM/INT/HUP. The exit
# code is the command's; a death by signal n is 128+n.
RUN_WRAPPER = r"""
import os, signal, subprocess, sys, threading
grace, cmd = float(sys.argv[1]), sys.argv[2:]
p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
def kill(sig):
    try:
        os.killpg(p.pid, sig)
    except OSError:
        pass
def stop(*_):
    kill(signal.SIGTERM)
    t = threading.Timer(grace, kill, (signal.SIGKILL,))
    t.daemon = True
    t.start()
def watch():
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass
    stop()
for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, stop)
threading.Thread(target=watch, daemon=True).start()
rc = p.wait()
kill(signal.SIGKILL)
sys.exit(128 - rc if rc < 0 else rc)
"""


def _sh(script: str) -> str:
    """The one remote command: a POSIX shell script, whatever the login shell of the target is."""
    return "sh -c " + shlex.quote(script)


def _rid(run_id: str) -> str:
    if not RUN_ID.match(run_id):
        raise ValueError("run id must be 32 hex digits")
    return run_id


def _dir(run_id: str) -> str:
    return 'D="$B/rt-%s"' % _rid(run_id)


def put_command(run_id: str) -> str:
    """Makes the run's private directory (0700, it must not exist), writes stdin (one tar: in.tar and the tool's own box in box/) into it and
    takes exactly the named members out of it. The ONLY thing sent besides the commands below."""
    members = " ".join(shlex.quote(x) for x in PUT_MEMBERS)
    return _sh(
        GUARD + ' && %s && mkdir -m 700 "$D" && cd "$D" && mkdir -m 700 out tmp data && umask 077 && cat > put.tar'
        " && tar -xf put.tar -- %s && rm -f put.tar" % (_dir(run_id), members)
    )


VERIFY = r"""
import hashlib, sys
for want, name in zip(sys.argv[1::2], sys.argv[2::2]):
    with open(name, "rb") as fh:
        if hashlib.sha256(fh.read()).hexdigest() != want:
            sys.exit("dead-code-checker: %s on the target is not the file that was sent" % name)
"""


def run_command(run_id: str, sha256: str, tool: Mapping[str, str]) -> str:
    """The ONE command of the class: the hashes of the tool's own files are checked in box/, then `python3 -I rtbox.py --on-target` runs from
    there, under `env -i` with a small allowlist and a private TMPDIR and HOME inside the run's directory, behind the wrapper that ends the
    process group when the channel closes. `tool` = name -> sha256 of each of TOOL_NAMES, as sent."""
    if (
        not SHA256.match(sha256)
        or sorted(tool) != sorted(TOOL_NAMES)
        or not all(SHA256.match(h) for h in tool.values())
    ):
        raise ValueError("sha256 must be 64 hex digits, and the tool hashes those of %s" % ", ".join(TOOL_NAMES))
    env = 'PATH=%s HOME="$D/data" TMPDIR="$D/tmp" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1' % SAFE_PATH
    guard = "python3 -I -c %s %d" % (shlex.quote(RUN_WRAPPER), WRAPPER_GRACE)
    check = "python3 -I -c %s %s" % (
        shlex.quote(VERIFY),
        " ".join("%s %s" % (tool[n], shlex.quote(n)) for n in TOOL_NAMES),
    )
    return _sh(
        GUARD_RO
        + ' && %s && test -f "$D/in.tar" && cd "$D/box" && %s && exec %s env -i %s python3 -I rtbox.py --on-target --box "$D" --sha256 %s'
        % (_dir(run_id), check, guard, env, sha256)
    )


def pull_command(run_id: str) -> str:
    """Read-only: the two fixed names of the run's out directory as one tar on stdout."""
    return _sh(GUARD_RO + ' && %s && cd "$D/out" && tar -cf - -- %s' % (_dir(run_id), " ".join(DELIVERY)))


def cleanup_command(run_id: str) -> str:
    return _sh(GUARD_RO + ' && %s && rm -rf -- "$D"' % _dir(run_id))


def probe_command() -> str:
    return _sh("echo dead-code-checker-ok")


# ---------------------------------------------------------------- ssh
class Target(NamedTuple):
    host: str
    user: str = ""
    port: int = 0

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


def ssh_argv(target: Target, remote: str, keep_stdin: bool = False, connect_timeout: int = 15) -> List[str]:
    """The ssh command line. The user's own config still names aliases and keys, but nothing in it can start a local command, forward a port
    or an agent, share a connection or add a remote command; a host key that is not in known_hosts is refused (never accepted)."""
    opts = (
        "BatchMode=yes", "StrictHostKeyChecking=yes", "ClearAllForwardings=yes", "ForwardAgent=no", "ForwardX11=no",
        "PermitLocalCommand=no", "ControlMaster=no", "ControlPath=none", "RemoteCommand=none", "RequestTTY=no",
        "ServerAliveInterval=15", "ServerAliveCountMax=3", f"ConnectTimeout={int(connect_timeout)}",
    )  # fmt: skip
    argv = ["ssh"] + ([] if keep_stdin else ["-n"])
    for o in opts:
        argv += ["-o", o]
    if target.port:
        argv += ["-p", str(target.port)]
    if target.user:
        argv += ["-l", target.user]
    return argv + ["--", target.host, remote]


_HOSTKEY = re.compile(
    r"host key verification failed|remote host identification has changed|no .* host key is known", re.I
)
_AUTH = re.compile(r"permission denied|too many authentication failures", re.I)


def _line(text: str, n: int = 160) -> str:
    """One line of what the target or ssh said, for a message: whitespace folded, secret-looking parts masked BEFORE the cut (a secret cut in
    the middle would survive as a prefix)."""
    return rtcfg.mask_secrets(" ".join(str(text).split()))[:n]


def explain(target: Target, stderr: str) -> str:
    """The one-line reason of an ssh failure: a host key that is not (or no longer) in known_hosts, a refused login, or the last line of ssh."""
    text = " ".join(stderr.split())
    if _HOSTKEY.search(text):
        return (
            f"the host key of {target.host} is not in your known_hosts (or it changed); this tool never accepts a new key: "
            f"check it and connect once with ssh by hand"
        )
    if _AUTH.search(text):
        return f"ssh to {target.label} was refused (key login only; no password is ever asked)"
    return _line((stderr.strip().splitlines() or ["no output"])[-1])


def _close(f) -> None:
    try:
        f.close()
    except (OSError, ValueError, AttributeError):
        pass


class SshTransport:
    """put / run / pull / cleanup of one run over the system `ssh` (`run=` and `popen=` are the seams of the tests). `timeouts` is the "timeouts"
    table of data/runtime.json, `max_bytes` the cap of a delivery."""

    def __init__(self, target: Target, timeouts: dict, max_bytes: int, run=None, popen=None, connect_timeout: int = 15):
        self.target, self.t, self.cap = target, timeouts, max_bytes * 2
        self._run, self._popen = run or safepath.run, popen or safepath.popen  # ssh is found through the absolute entries of PATH only
        self.label, self.name, self.connect = target.label, target.label, connect_timeout

    def _argv(self, remote: str, keep_stdin: bool = False) -> List[str]:
        return ssh_argv(self.target, remote, keep_stdin, self.connect)

    def reachable(self) -> Tuple[bool, str]:
        try:
            p = self._run(self._argv(probe_command()), capture_output=True, timeout=self.connect + 30)
        except (OSError, subprocess.SubprocessError) as e:
            return False, _line(f"ssh: {e}")
        if p.returncode == 0 and b"dead-code-checker-ok" in (p.stdout or b""):
            return True, "ok"
        return False, explain(self.target, (p.stderr or b"").decode("utf-8", "replace"))

    def put(self, run_id: str, data: bytes) -> Tuple[int, str]:
        try:
            p = self._run(
                self._argv(put_command(run_id), keep_stdin=True),
                input=data,
                capture_output=True,
                timeout=self.t["put_s"],
            )
        except (OSError, subprocess.SubprocessError) as e:
            return 255, _line(f"ssh: {e}")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, (
            explain(self.target, err) if p.returncode == 255 else _line((err.strip().splitlines() or ["no output"])[-1])
        )

    def run(self, run_id: str, sha256: str, wait_s: float, tool: Mapping[str, str]) -> Tuple[int, str]:
        """(exit code, last output line). 255 = no transport; 124 = the wait ran out (the channel is closed, the target kills the group)."""
        try:  # stdin stays an open pipe for the whole run: its EOF is how the wrapper on the target learns that the channel is gone
            proc = self._popen(
                self._argv(run_command(run_id, sha256, tool), keep_stdin=True),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            return 255, _line(f"ssh: {e}")
        lines: List[str] = []

        def pump():
            try:
                for raw in proc.stdout:
                    lines.append(raw.decode("utf-8", "replace").rstrip("\n"))
                    del lines[:-20]
            except (OSError, ValueError):
                pass

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            rc = proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            _close(proc.stdin)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            return 124, "no answer within %d s" % wait_s
        finally:
            _close(proc.stdin)
        th.join(5)
        if rc == 255:
            return 255, explain(self.target, "\n".join(lines))
        return rc, (_line(lines[-1]) if lines else "no output")

    def pull(self, run_id: str) -> Tuple[Optional[bytes], str]:
        try:
            proc = self._popen(
                self._argv(pull_command(run_id)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            return None, _line(f"ssh: {e}")
        timer = threading.Timer(self.t["pull_s"], proc.kill)
        timer.daemon = True
        timer.start()
        try:
            data = proc.stdout.read(self.cap + 1)
            err = proc.stderr.read(2000).decode("utf-8", "replace").strip()
            rc = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            _close(proc.stdout)
            _close(proc.stderr)
        if len(data) > self.cap:
            return None, f"the delivery is larger than {self.cap} bytes"
        if rc != 0:
            return None, "pull failed (rc %s): %s" % (rc, _line(err.splitlines()[-1]) if err else "no output")
        return data, ""

    def cleanup(self, run_id: str) -> None:
        try:
            self._run(
                self._argv(cleanup_command(run_id)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            pass


# ---------------------------------------------------------------- secrets: what never goes into the project copy
SECRET_NAME_GLOBS = (
    ".env*", "*.pem", "*.key", "id_*", "*.gpg", "credentials*", "*.p12", "*.pfx", "*.kdbx", ".netrc", ".pgpass", "*.asc",
    ".git-credentials", "*.ppk", "*.jks", "*.keystore", ".npmrc", ".pypirc", "*.env", "*.ovpn",
)  # fmt: skip
SECRET_NAME_RES = (re.compile(r"^(?:.*[-_.])?kubeconfig(?:\.(?:ya?ml|json|conf|cfg|txt))?$", re.I),)
SECRET_DIR_NAMES = (".password-store", ".gnupg", ".ssh", ".kube")
TEMPLATE_SUFFIXES = (
    ".example",
    ".sample",
    ".template",
    ".dist",
    ".tpl",
)  # `.env.example`: a template has no secret by its name (its content is still scanned)


def secret_name_hits(paths) -> List[str]:
    out = []
    for rel in paths:
        parts = rel.split("/")
        base = parts[-1].lower()
        if base.endswith(TEMPLATE_SUFFIXES):
            continue
        if (
            any(d in parts[:-1] or base == d for d in SECRET_DIR_NAMES)
            or any(fnmatch.fnmatch(base, g) for g in SECRET_NAME_GLOBS)
            or any(r.match(base) for r in SECRET_NAME_RES)
        ):
            out.append(rel)
    return out


REGEX_RULES = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("api-key-sk", re.compile(r"\bsk-[a-z]{2,8}-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "env-assignment",
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z0-9_]*(?:API_?KEY|SECRET|PASSWD|PASSWORD|TOKEN|PRIVATE_KEY)"
            r"[A-Z0-9_]*[ \t]*=[ \t]*(?![$\"'`(~/.])(?=[A-Za-z0-9/+_.=-]*[0-9])"
            r"[A-Za-z0-9+_=-][A-Za-z0-9/+_.=-]{19,}[ \t]*(?:#.*)?$"
        ),
    ),
    (
        "generic-secret",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b["']?\s*[:=]\s*["'][A-Za-z0-9/+_.=-]{24,}["']"""
        ),
    ),
)
SCAN_CHUNK = 4 * 1024 * 1024
SCAN_OVERLAP = 1024
SCAN_HARD_MAX = 64 * 1024 * 1024
SCAN_ARCHIVE_BUDGET = 256 * 1024 * 1024
SCAN_ARCHIVE_MEMBERS = 20000
SCAN_ARCHIVE_DEPTH = 3


class _Budget:
    def __init__(self):
        self.used = 0
        self.members = 0


def _archive_kind(head: bytes) -> str:
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[257:262] == b"ustar":
        return "tar"
    return ""


def _rules_in(data: bytes, found: set) -> None:
    """Every rule name that matches somewhere in data (chunked, overlapping; latin-1 keeps every byte)."""
    pos, n = 0, len(data)
    while True:
        text = data[pos : pos + SCAN_CHUNK + SCAN_OVERLAP].decode("latin-1")
        for name, rx in REGEX_RULES:
            if name not in found and rx.search(text):
                found.add(name)
        pos += SCAN_CHUNK
        if pos >= n:
            return


def _scan_blob(data: bytes, found: set, bud: _Budget, depth: int) -> None:
    """Regex scan of data and, for zip / tar / gzip / bzip2 / xz content, of everything inside. Anything that cannot be read in full is
    refused (a rule name), never skipped."""
    bud.used += len(data)
    if bud.used > SCAN_ARCHIVE_BUDGET or bud.members > SCAN_ARCHIVE_MEMBERS:
        found.add("archive-too-large-to-scan")
        return
    _rules_in(data, found)
    kind = _archive_kind(data[:512])
    if not kind:
        return
    if depth >= SCAN_ARCHIVE_DEPTH:
        found.add("archive-too-deep-to-scan")
        return
    try:
        if kind == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    bud.members += 1
                    if info.flag_bits & 1:
                        found.add("archive-encrypted")
                    elif info.file_size > SCAN_HARD_MAX or bud.used + info.file_size > SCAN_ARCHIVE_BUDGET:
                        found.add("archive-too-large-to-scan")
                    else:
                        _scan_blob(z.read(info), found, bud, depth + 1)
                    if bud.members > SCAN_ARCHIVE_MEMBERS:
                        found.add("archive-too-large-to-scan")
                        return
            return
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        except tarfile.TarError:  # a single compressed stream, not a tar
            opener = {"gz": gzip.GzipFile, "bz2": bz2.BZ2File, "xz": lzma.LZMAFile}.get(kind)
            if opener is None:
                return
            left = SCAN_ARCHIVE_BUDGET - bud.used
            inner = opener(fileobj=io.BytesIO(data)).read(left + 1)
            if len(inner) > left:
                found.add("archive-too-large-to-scan")
            else:
                _scan_blob(inner, found, bud, depth + 1)
            return
        with tf:
            for m in tf:
                if not m.isfile():
                    continue
                bud.members += 1
                if (
                    bud.members > SCAN_ARCHIVE_MEMBERS
                    or m.size > SCAN_HARD_MAX
                    or bud.used + m.size > SCAN_ARCHIVE_BUDGET
                ):
                    found.add("archive-too-large-to-scan")
                    return
                fh = tf.extractfile(m)
                if fh is not None:
                    _scan_blob(fh.read(), found, bud, depth + 1)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, zlib.error, lzma.LZMAError):
        return  # not (or no longer) an archive: the raw bytes were scanned above
    except (NotImplementedError, RuntimeError, OSError, ValueError, OverflowError):
        found.add("archive-unreadable")  # an unsupported method or a password: its content stays unseen


def scan_content(data: bytes) -> set:
    """The secret rules that match `data` (empty = clean)."""
    found: set = set()
    if len(data) > SCAN_HARD_MAX:
        return {"too-large-to-scan"}
    _scan_blob(data, found, _Budget(), 0)
    return found
