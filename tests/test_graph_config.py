"""dead_code_checker.graph.config: the [graph] table, the variables, waive and disable rules.

python3 -m pytest -q tests/test_graph_config.py
"""

import datetime as dt
import textwrap

import pytest

from dead_code_checker.graph import config

TODAY = dt.date(2026, 11, 13)


def mk(tmp_path, body, env=None, **kw):
    (tmp_path / ".dead-code-checker.toml").write_text("[graph]\n" + textwrap.dedent(body))
    return config.load(str(tmp_path), environ=env or {}, today=TODAY, **kw)


def codes(c, level=None):
    return sorted(p.code for p in c.problems if level in (None, p.level))


def test_defaults_without_a_file(tmp_path):
    c = config.load(str(tmp_path), environ={}, today=TODAY)
    assert c.problems == () and c.source == "" and c.mode == "closed_world" and c.enabled
    assert c.budget("commit") == 20 and c.budget("branch") == 900
    assert c.viewer_code is True and c.viewer_code_bytes == 6 * 1024 * 1024 and c.waives == ()
    assert not c.is_waived("dc.dead", path="a.py") and c.disabled == config.Disabled(False, None, None, False)


def test_the_text_checker_table_is_not_read(tmp_path):
    (tmp_path / ".dead-code-checker.toml").write_text('[deadcode]\nfunction_level = "E"\nmode = "nonsense"\n')
    c = config.load(str(tmp_path), environ={}, today=TODAY)
    assert c.problems == () and c.source == config.FILE


def test_use_file_false_reads_nothing(tmp_path):
    assert mk(tmp_path, 'mode = "bogus"\n', use_file=False).problems == ()


def test_mode(tmp_path):
    assert mk(tmp_path, 'mode = "library"\n').mode == "library"
    c = mk(tmp_path, 'mode = "open"\n')
    assert codes(c, "E") == ["dc.bad-config"] and c.mode == "closed_world"


def test_unknown_key_is_an_error(tmp_path):
    c = mk(tmp_path, "frobnicate = 1\n")
    assert codes(c, "E") == ["dc.bad-config"] and "frobnicate" in c.problems[0].msg


@pytest.mark.parametrize("key", config.FORBIDDEN)
def test_a_repository_cannot_choose_what_runs_or_where_data_goes(tmp_path, key):
    c = mk(tmp_path, f'{key} = "x"\n')
    assert codes(c, "E") == ["dc.bad-config"] and "not allowed" in c.problems[0].msg


def test_broken_toml_is_an_error_and_defaults_stay(tmp_path):
    (tmp_path / config.FILE).write_text("[graph\nmode =")
    c = config.load(str(tmp_path), environ={}, today=TODAY)
    assert codes(c, "E") == ["dc.bad-config"] and c.mode == "closed_world"


def test_graph_must_be_a_table(tmp_path):
    (tmp_path / config.FILE).write_text("graph = 3\n")
    assert codes(config.load(str(tmp_path), environ={}, today=TODAY), "E") == ["dc.bad-config"]


def test_a_symlink_or_a_fifo_or_a_huge_file_is_not_read(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text('[graph]\nmode = "library"\n')
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / config.FILE).symlink_to(outside)
    c = config.load(str(repo), environ={}, today=TODAY)
    assert c.mode == "closed_world" and codes(c, "W") == ["dc.bad-config"]
    (repo / config.FILE).unlink()
    (repo / config.FILE).write_text("# " + "x" * config.MAX_BYTES + "\n")
    assert codes(config.load(str(repo), environ={}, today=TODAY), "W") == ["dc.bad-config"]


def test_a_fifo_is_not_opened(tmp_path):
    import os

    os.mkfifo(tmp_path / config.FILE)
    c = config.load(str(tmp_path), environ={}, today=TODAY)  # would hang if it were opened
    assert codes(c, "W") == ["dc.bad-config"]


def test_disable_needs_reason_and_date(tmp_path):
    c = mk(tmp_path, "enabled = false\n")
    assert codes(c, "E") == ["dc.bad-config"] and c.enabled
    c = mk(tmp_path, 'enabled = false\ndisabled_reason = "x"\n')
    assert codes(c, "E") == ["dc.bad-config"] and c.enabled


def test_valid_disable_is_off_and_a_warning_every_time(tmp_path):
    c = mk(tmp_path, 'enabled = false\ndisabled_reason = "migration"\ndisabled_until = 2026-12-01\n')
    assert not c.enabled and codes(c) == ["dc.disabled"] and c.problems[0].level == "W"
    assert c.disabled == config.Disabled(True, "migration", dt.date(2026, 12, 1), False)
    c = mk(tmp_path, 'enabled = false\ndisabled_reason = "m"\ndisabled_until = "2026-11-13"\n')  # the day itself counts
    assert not c.enabled


def test_expired_disable_runs_again_with_a_warning(tmp_path):
    c = mk(tmp_path, 'enabled = false\ndisabled_reason = "m"\ndisabled_until = 2026-11-12\n')
    assert c.enabled and codes(c) == ["dc.disabled-expired"] and c.disabled.expired


def test_disable_keys_without_enabled_false(tmp_path):
    assert codes(mk(tmp_path, 'disabled_reason = "x"\n'), "E") == ["dc.bad-config"]
    assert codes(mk(tmp_path, "enabled = true\ndisabled_until = 2027-01-01\n"), "E") == ["dc.bad-config"]
    c = mk(tmp_path, 'enabled = "no"\n')
    assert c.enabled and codes(c, "W") == ["dc.bad-config"]


def test_budget_in_the_file_is_clamped_and_checked(tmp_path):
    c = mk(tmp_path, "[graph.budget]\ncommit_s = 60\nbranch_s = 99999\nstop_s = 1\n")
    assert c.budget("commit") == 60 and c.budget("branch") == 3600
    assert codes(c) == ["dc.bad-budget", "dc.bad-budget"] and {p.level for p in c.problems} == {"W"}
    c = mk(tmp_path, "[graph.budget]\ncommit_s = -1\nbranch_s = true\n")
    assert c.budget("commit") == 20 and c.budget("branch") == 900 and codes(c) == ["dc.bad-budget"] * 2
    assert codes(mk(tmp_path, "budget = 5\n")) == ["dc.bad-budget"]


def test_viewer_keys(tmp_path):
    c = mk(tmp_path, "viewer_code = false\nviewer_code_bytes = 1000\n")
    assert c.viewer_code is False and c.viewer_code_bytes == 1000 and c.problems == ()
    c = mk(tmp_path, 'viewer_code = "no"\nviewer_code_bytes = 0\n')
    assert c.viewer_code is True and c.viewer_code_bytes == 6 * 1024 * 1024 and codes(c) == ["dc.bad-config"] * 2
    assert codes(mk(tmp_path, "viewer_code_bytes = true\n"), "W") == ["dc.bad-config"]


WAIVE = """
    [[graph.waive]]
    code = "dc.dead"
    path_glob = "legacy/*.py"
    reason = "removed in the rewrite"
    until = 2026-12-31
    [[graph.waive]]
    code = "dc.engine-missing"
    engine = "ruff"
    reason = "not installed on CI"
    until = "2026-11-20"
"""


def test_waive_by_glob_and_by_engine(tmp_path):
    c = mk(tmp_path, WAIVE)
    assert c.problems == () and len(c.waives) == 2
    assert c.is_waived("dc.dead", path="legacy/old.py") and c.is_waived("dc.dead", path="./legacy/old.py")
    assert not c.is_waived("dc.dead", path="src/old.py") and not c.is_waived("dc.dead")
    assert not c.is_waived("dc.other", path="legacy/old.py")
    assert c.is_waived("dc.engine-missing", engine="ruff") and not c.is_waived("dc.engine-missing", engine="mypy")
    assert not c.is_waived("dc.engine-missing", path="legacy/old.py")


def test_a_waive_stops_at_its_date(tmp_path):
    c = mk(tmp_path, WAIVE)
    late = c._replace(today=dt.date(2026, 11, 21))
    assert not late.is_waived("dc.engine-missing", engine="ruff") and late.is_waived("dc.dead", path="legacy/a.py")
    expired = mk(tmp_path, WAIVE.replace("2026-12-31", "2026-11-01"))
    assert codes(expired, "E") == ["dc.waive-expired"] and not expired.is_waived("dc.dead", path="legacy/a.py")


@pytest.mark.parametrize(
    "body",
    [
        'waive = "x"\n',
        "waive = [1]\n",
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "a"\nreason = "r"\nuntil = 2027-01-01\nextra = 1\n',
        '[[graph.waive]]\npath_glob = "a"\nreason = "r"\nuntil = 2027-01-01\n',
        '[[graph.waive]]\ncode = "dc.bad-config"\npath_glob = "a"\nreason = "r"\nuntil = 2027-01-01\n',
        '[[graph.waive]]\ncode = "dc.dead"\nreason = "r"\nuntil = 2027-01-01\n',
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "a"\nengine = "e"\nreason = "r"\nuntil = 2027-01-01\n',
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "a"\nuntil = 2027-01-01\n',
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "a"\nreason = "  "\nuntil = 2027-01-01\n',
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "a"\nreason = "r"\n',
        '[[graph.waive]]\ncode = "dc.dead"\npath_glob = "a"\nreason = "r"\nuntil = "soon"\n',
    ],
)
def test_a_bad_waive_is_an_error_and_waives_nothing(tmp_path, body):
    c = mk(tmp_path, body)
    assert codes(c, "E") == ["dc.bad-config"] and c.waives == ()


def test_bad_config_cannot_be_waived(tmp_path):
    c = mk(tmp_path, WAIVE)._replace(waives=(config.Waive("dc.bad-config", "*", None, "r", dt.date(2030, 1, 1)),))
    assert not c.is_waived("dc.bad-config", path="x")


def test_variables_override_the_file(tmp_path):
    env = {
        "DEAD_CODE_CHECKER_MODE": "library",
        "DEAD_CODE_CHECKER_VIEWER_CODE": "off",
        "DEAD_CODE_CHECKER_VIEWER_CODE_BYTES": "4096",
        "DEAD_CODE_CHECKER_BUDGET_COMMIT_S": "300",
        "DEAD_CODE_CHECKER_BUDGET_BRANCH_S": "7200.5",
    }
    c = mk(tmp_path, 'mode = "closed_world"\nviewer_code = true\n[graph.budget]\ncommit_s = 30\n', env)
    assert c.problems == () and c.mode == "library" and c.viewer_code is False and c.viewer_code_bytes == 4096
    assert c.budget("commit") == 300 and c.budget("branch") == 7200.5  # the user's own values are not clamped


def test_variables_work_without_a_file(tmp_path):
    c = config.load(
        str(tmp_path), environ={"DEAD_CODE_CHECKER_VIEWER_CODE": "TRUE", "DEAD_CODE_CHECKER_MODE": "library"}
    )
    assert c.mode == "library" and c.viewer_code is True and c.source == ""


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODE", "open"),
        ("VIEWER_CODE", "maybe"),
        ("VIEWER_CODE_BYTES", "0"),
        ("VIEWER_CODE_BYTES", "-5"),
        ("VIEWER_CODE_BYTES", "1e3"),
        ("BUDGET_COMMIT_S", "soon"),
        ("BUDGET_BRANCH_S", "0"),
        ("BUDGET_BRANCH_S", "inf"),
    ],
)
def test_a_bad_variable_is_a_warning_and_ignored(tmp_path, name, value):
    c = mk(tmp_path, 'mode = "library"\n', {"DEAD_CODE_CHECKER_" + name: value})
    assert [(p.level, p.where) for p in c.problems] == [("W", "DEAD_CODE_CHECKER_" + name)]
    assert c.mode == "library" and c.budget("commit") == 20 and c.budget("branch") == 900 and c.viewer_code


def test_empty_variables_are_unset(tmp_path):
    c = config.load(str(tmp_path), environ={"DEAD_CODE_CHECKER_MODE": "  "}, today=TODAY)
    assert c.problems == () and c.mode == "closed_world"


def test_the_process_environment_is_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DEAD_CODE_CHECKER_MODE", "library")
    assert config.load(str(tmp_path)).mode == "library"


def test_as_dict_is_json_ready_and_stable(tmp_path):
    import json

    c = mk(tmp_path, WAIVE + "\n")
    d = c.as_dict()
    assert json.dumps(d, sort_keys=True) == json.dumps(mk(tmp_path, WAIVE + "\n").as_dict(), sort_keys=True)
    assert (
        d["config_file"] == config.FILE
        and d["waive"][0]["until"] == "2026-12-31"
        and d["budget"] == {"branch_s": 900.0, "commit_s": 20.0}
    )


def test_problem_render():
    p = config.Problem("E", ".dead-code-checker.toml", "dc.bad-config", "bad mode 'x'")
    assert p.render() == "dead-code-checker: E .dead-code-checker.toml: dc.bad-config: bad mode 'x'"


def test_to_date():
    assert config.to_date(dt.datetime(2026, 1, 2, 3)) == dt.date(2026, 1, 2) and config.to_date(
        " 2026-01-02 "
    ) == dt.date(2026, 1, 2)
    assert config.to_date("2026-13-01") is None and config.to_date(5) is None and config.to_date(None) is None
