"""dead_code_checker.graph.par: workers and ordered parallel units.

python3 -m pytest -q tests/test_graph_par.py
"""

import time

import pytest

from dead_code_checker.graph import par


def square(n):
    return n * n


def slow_first(n):
    time.sleep(0.2 if n == 0 else 0)  # the first unit finishes last
    return (n, n * 10)


def boom(n):
    if n in (2, 5):
        raise ValueError(f"unit {n}")
    return n


def test_workers_flag_beats_env_beats_default():
    assert par.workers(3, {par.ENV_JOBS: "7"}) == 3
    assert par.workers(None, {par.ENV_JOBS: "7"}) == 7
    assert 1 <= par.workers(None, {}) <= par.MAX_DEFAULT


@pytest.mark.parametrize("bad", ["", "x", "0", "-2", "1.5"])
def test_workers_ignores_a_bad_env_value(bad):
    assert par.workers(None, {par.ENV_JOBS: bad}) == par.workers(None, {})


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_workers_rejects_a_bad_flag(bad):
    with pytest.raises(ValueError):
        par.workers(bad)


def test_workers_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(par.ENV_JOBS, "5")
    assert par.workers() == 5


@pytest.mark.parametrize("kind", ["process", "thread"])
@pytest.mark.parametrize("jobs", [1, 2, 4])
def test_result_is_in_unit_order_whatever_finishes_first(kind, jobs):
    units = list(range(6))
    assert par.run_units(units, slow_first, jobs=jobs, kind=kind) == [(n, n * 10) for n in units]


def test_same_result_for_every_worker_count():
    units = [5, 3, 9, 1, 7, 2, 8]
    runs = [par.run_units(units, square, jobs=j) for j in (1, 2, 3, 8)]
    assert all(r == runs[0] for r in runs) and runs[0] == [25, 9, 81, 1, 49, 4, 64]


def test_key_orders_the_units_before_they_run():
    assert par.run_units(["b", "c", "a"], str.upper, jobs=2, key=lambda s: s, kind="thread") == ["A", "B", "C"]
    assert par.run_units([3, 1, 2], square, jobs=2, key=lambda n: -n) == [9, 4, 1]


def test_unit_order_is_kept_for_unsortable_units():
    units = [{"a": 1}, [2], "x", None]
    assert par.run_units(units, repr, jobs=2, kind="thread") == [repr(u) for u in units]


def test_no_units_is_an_empty_result():
    assert par.run_units([], square, jobs=4) == []
    assert par.run_units(iter(()), square) == []


def test_a_generator_of_units_works():
    assert par.run_units((n for n in range(4)), square, jobs=2) == [0, 1, 4, 9]


def test_the_first_failing_unit_in_order_is_raised():
    with pytest.raises(ValueError, match="unit 2"):
        par.run_units(range(8), boom, jobs=4)
    with pytest.raises(ValueError, match="unit 2"):
        par.run_units(range(8), boom, jobs=1, kind="thread")


def test_threads_take_closures_and_share_memory():
    seen = []
    assert par.run_units(range(5), lambda n: seen.append(n) or n + 1, jobs=3, kind="thread") == [1, 2, 3, 4, 5]
    assert sorted(seen) == [0, 1, 2, 3, 4]


def test_a_bad_kind_or_jobs_is_a_value_error():
    with pytest.raises(ValueError, match="kind"):
        par.run_units([1], square, kind="fiber")
    with pytest.raises(ValueError, match="workers"):
        par.run_units([1], square, jobs=0)


def test_jobs_default_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(par.ENV_JOBS, "2")
    assert par.run_units([1, 2, 3], square) == [1, 4, 9]
