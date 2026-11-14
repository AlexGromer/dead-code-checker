"""graph.par: run independent units in parallel, with a result that does not depend on the worker count.

    workers(flag=None, environ=None) -> int        --jobs, else $DEAD_CODE_CHECKER_JOBS, else the CPUs (at most 8)
    run_units(units, fn, jobs=None, key=None, kind="process") -> list

Every call goes through one executor path (a single worker is a pool of one, never a separate serial branch), so a
result that differs between `--jobs 1` and `--jobs 8` is a bug in the unit, not in the scheduler. Results come back in
the order of `units` (or of `key(unit)` when a key is given), never in completion order. The first failing unit in that
order re-raises after the pending ones are cancelled.

A process pool needs a picklable `fn` (a module-level function) and picklable units; `kind="thread"` has no such
limit and suits units that mostly wait on a subprocess.
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import Any, Callable, Iterable, List, Mapping, Optional

ENV_JOBS = "DEAD_CODE_CHECKER_JOBS"
MAX_DEFAULT = 8
KINDS = ("process", "thread")


def _cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # honours cpusets and taskset, unlike cpu_count
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def workers(flag: Optional[int] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    """The worker count: the flag, else the environment variable, else min(CPUs, 8).

    A flag below 1 is a caller error (ValueError); an environment value that is not an integer >= 1 is ignored."""
    if flag is not None:
        if not isinstance(flag, int) or isinstance(flag, bool) or flag < 1:
            raise ValueError(f"workers must be an integer >= 1, got {flag!r}")
        return flag
    env = os.environ if environ is None else environ
    try:
        n = int(str(env.get(ENV_JOBS, "")).strip())
    except ValueError:
        n = 0
    return n if n >= 1 else min(_cpus(), MAX_DEFAULT)


def run_units(
    units: Iterable[Any],
    fn: Callable[[Any], Any],
    *,
    jobs: Optional[int] = None,
    key: Optional[Callable[[Any], Any]] = None,
    kind: str = "process",
) -> List[Any]:
    """fn over every unit on `jobs` workers (see workers()); the results ordered like `units`,
    or like sorted(units, key=key) when a key is given."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    n = workers(jobs)
    ordered = list(units) if key is None else sorted(units, key=key)
    if not ordered:
        return []
    pool = concurrent.futures.ProcessPoolExecutor if kind == "process" else concurrent.futures.ThreadPoolExecutor
    with pool(max_workers=min(n, len(ordered))) as ex:
        futs = [ex.submit(fn, u) for u in ordered]
        out: List[Any] = []
        try:
            for f in futs:
                out.append(f.result())
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    return out
