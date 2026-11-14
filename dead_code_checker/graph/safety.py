"""graph.safety: the checker never breaks a commit or a build. Only the policy findings (a registration that points into the void,
a missing engine) may be E. A failure of the checker itself (an exception, a crash, a store error), a budget stop and a missing
base are at most a W, and the finished results are kept. Everything the levels share for that is here.

    reason(exc) -> str                one line that names the exception
    check_failed(level, exc) -> row   the W dc.check-failed row of a failure; a row is (level, path, line, code, msg)
    pre_existing(found) -> found      the branch level with no base: every E about the tree becomes a W, in place
"""

from __future__ import annotations

from typing import List

WHERE = "(dead-code-checker)"  # the path of a finding about the run, not about a file


def reason(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:200]


def check_failed(level: str, exc: BaseException) -> tuple:
    msg = f"the {level} check failed ({reason(exc)}); it was skipped, nothing is blocked"
    return ("W", WHERE, 0, "dc.check-failed", msg)


def pre_existing(found: List) -> List:
    """The branch level with no base: the whole tree is the diff, so no finding was introduced by it. Every E about the tree (a
    dangling root, a dead symbol) is a W, in place; dc.engine-missing stays: it is about this machine, not about the tree.
    """
    for f in found:
        if f.level == "E" and f.code != "dc.engine-missing":
            f.level, f.msg = "W", f"{f.msg} (pre-existing: there is no base to compare with, nothing is blocked)"
    return found
