"""graph.rtcfg: the `[graph.runtime]` table, the host rules, the allow-list, the secret mask and the data of the runtime mode.

python3 -m pytest -q tests/test_graph_rtcfg.py

Nothing here contacts a host or runs a command: the module only parses and checks text.
"""

import datetime as dt
import json
import subprocess
import sys

import pytest

from dead_code_checker.graph import config, rtcfg

TODAY = dt.date(2026, 11, 13)


def mk(tmp_path, body):
    (tmp_path / ".dead-code-checker.toml").write_text("[graph.runtime]\n" + body)
    return config.load(str(tmp_path), environ={}, today=TODAY)


def test_defaults_are_off():
    cfg, probs = rtcfg.parse({})
    assert cfg == rtcfg.OFF and probs == [] and not cfg.enabled
    assert cfg.host == "" and cfg.command == () and not hasattr(cfg, "user") and not hasattr(cfg, "port")
    d = rtcfg.data()
    assert cfg.wall_s() == d["timeouts"]["default_s"] and cfg.refresh() == d["refresh_days"]


def test_a_full_table():
    raw = {"enabled": True, "host": "ci-box", "command": "python3 -m pytest -q 'tests/a b'", "timeout": 300, "refresh_days": 3}
    cfg, probs = rtcfg.parse(raw)
    assert probs == [] and cfg == rtcfg.RuntimeCfg(True, "ci-box", ("python3", "-m", "pytest", "-q", "tests/a b"), 300, 3)
    assert cfg.wall_s() == 300 and cfg.refresh() == 3


def test_the_command_is_split_without_a_shell():
    cfg, probs = rtcfg.parse({"command": "make test && rm -rf x"})
    assert probs == [] and cfg.command == ("make", "test", "&&", "rm", "-rf", "x")  # words, never a shell line
    assert rtcfg.parse({"command": ["go", "test", "./..."]})[0].command == ("go", "test", "./...")


@pytest.mark.parametrize("bad", ["", [], ["a", ""], ["a", 1], 5, "unterminated 'quote", ["a\nb"], ["a\0b"], ["x"] * 65, ["y" * 5000]])
def test_a_bad_command_falls_back_to_the_default(bad):
    cfg, probs = rtcfg.parse({"enabled": True, "command": bad})
    assert cfg.command == () and cfg.enabled and len(probs) == 1 and "'command'" in probs[0]


def test_timeout_is_cut_at_the_wall_and_bad_numbers_fall_back():
    wall = rtcfg.data()["limits"]["wall_s"]
    assert rtcfg.parse({"timeout": wall * 10})[0].wall_s() == wall
    for bad in (0, -1, 1.5, True, "9"):
        cfg, probs = rtcfg.parse({"timeout": bad, "refresh_days": bad})
        assert cfg.timeout == 0 and cfg.refresh_days == 0 and len(probs) == 2


def test_unknown_keys_are_problems():
    cfg, probs = rtcfg.parse({"enabled": True, "target": "box2", "ssh_key": "x"})
    assert cfg.enabled and len(probs) == 2 and "'ssh_key'" in probs[0] and "'target'" in probs[1]


def test_enabled_must_be_a_bool_and_the_table_a_table():
    cfg, probs = rtcfg.parse({"enabled": "yes", "host": "h"})
    assert cfg == rtcfg.OFF and "stays off" in probs[-1]
    cfg, probs = rtcfg.parse("host")
    assert cfg == rtcfg.OFF and len(probs) == 1


@pytest.mark.parametrize("host", ["ci", "ci-box.example.org", "my_box", "[fe80::1]", "fe80::1", "a" * 253, "Host1"])
def test_good_hosts(host):
    assert rtcfg.host_problem(host) == ""
    cfg, probs = rtcfg.parse({"enabled": True, "host": host})
    assert probs == [] and cfg.host == host


@pytest.mark.parametrize(
    "host",
    ["", "-oProxyCommand=x", "-h", "a b", "a\tb", "a\nb", "a\0b", "a;b", "a$(x)", "a`x`", "a/b", "a@b", "ho st", "a" * 254, "é", "a,b", 5, None, ["h"]],
)
def test_bad_hosts_leave_the_mode_off(host):
    assert rtcfg.host_problem(host)
    cfg, probs = rtcfg.parse({"enabled": True, "host": host, "command": "x"})
    assert cfg == rtcfg.OFF and "'host'" in probs[0] and probs[-1].endswith("stays off")


@pytest.mark.parametrize("raw", [{"user": "root"}, {"port": 2222}, {"user": "root", "port": 22}, {"user": "a b", "port": 99999}, {"user": 5, "port": "x"}])
def test_user_and_port_of_the_repository_file_are_ignored_with_one_note(raw):
    """The user approves a host, not a login: a file that is somebody else's cannot choose who the tool logs in as, or where."""
    cfg, probs = rtcfg.parse({"enabled": True, "host": "h", **raw})
    assert cfg == rtcfg.RuntimeCfg(True, "h") and not hasattr(cfg, "user") and not hasattr(cfg, "port")  # nothing of the login is kept
    assert len(probs) == 1 and "ignored" in probs[0] and all(repr(k) in probs[0] for k in raw)


def test_the_allowed_entries_carry_the_users_login():
    env = {rtcfg.ENV_HOSTS: "ci@Box1:2222, box2 ,tester@[::1]:22, fe80::1, ci2@box3:99999, a b@x, -bad:22, box1:1"}
    assert rtcfg.allowed_logins(env) == {
        "box1": rtcfg.Login("ci", 2222),  # the first entry of a host wins
        "box2": rtcfg.Login(),
        "[::1]": rtcfg.Login("tester", 22),
        "fe80::1": rtcfg.Login(),  # a bare IPv6 host has no port
    }
    assert rtcfg.hosts_allowed(env) == {"box1", "box2", "[::1]", "fe80::1"} and rtcfg.host_allowed("BOX1", env)
    assert rtcfg.parse_entry("u@h:22") == ("u", "h", 22, "") and rtcfg.parse_entry("h")[:3] == ("", "h", 0)
    assert rtcfg.parse_entry("u@h:0")[3] and rtcfg.parse_entry("1u@h")[3] and rtcfg.parse_entry("h h")[3]


def test_the_allow_list_comes_from_the_variable_only():
    env = {rtcfg.ENV_HOSTS: " Box1 , box2,,-bad,a b ,[::1] "}
    assert rtcfg.hosts_allowed(env) == {"box1", "box2", "[::1]"}
    assert rtcfg.host_allowed("BOX1", env) and rtcfg.host_allowed("box2", env) and not rtcfg.host_allowed("box3", env)
    assert not rtcfg.host_allowed("", env) and not rtcfg.host_allowed(None, env)
    assert rtcfg.hosts_allowed({}) == set() and rtcfg.hosts_allowed({rtcfg.ENV_HOSTS: ""}) == set()


def test_the_allow_list_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(rtcfg.ENV_HOSTS, "z1")
    assert rtcfg.hosts_allowed() == {"z1"}
    monkeypatch.delenv(rtcfg.ENV_HOSTS)
    assert rtcfg.hosts_allowed() == set()


def test_no_local_target_is_offered():
    # the table has no way to say "run here": a loopback-looking value is a plain host name for the other half to refuse, and the
    # old names of other kinds of target are unknown keys
    assert "target" not in rtcfg.KEYS and "local" not in rtcfg.KEYS
    cfg, probs = rtcfg.parse({"enabled": True, "target": "local"})
    assert len(probs) == 1 and "'target'" in probs[0]


def test_skip_line_is_one_short_line():
    assert rtcfg.skip_line() == rtcfg.SKIP
    line = rtcfg.skip_line("no bwrap\non the host\n" + "x" * 1000)
    assert "\n" not in line and line.startswith(rtcfg.SKIP + " (no bwrap on the host") and len(line) <= len(rtcfg.SKIP) + rtcfg.data()["result"]["note_chars"] + 4


def test_argv_problem():
    assert rtcfg.argv_problem(["a", "b"]) == "" and rtcfg.argv_problem(("a",)) == ""
    for bad in (None, "a", [], [""], [1], ["a\nb"], ["a\rb"], ["a\0"], ["x"] * 65, ["x" * 4097]):
        assert rtcfg.argv_problem(bad)


# a token-shaped fixture that is no secret
FAKE = {
    "sk": "sk-" + "fakefakefakefakefakefake",
    "gh": "ghp_" + "F" * 24,
    "aws": "AKIA" + "F" * 16,
    "aiza": "AIza" + "f" * 30,
    "jwt": "eyJfakefake1.eyJfakefake2.fakesig1",
}


@pytest.mark.parametrize("key", sorted(FAKE))
def test_mask_secrets_tokens(key):
    out = rtcfg.mask_secrets(f"fail: used {FAKE[key]} here")
    assert FAKE[key] not in out and "***" in out and out.startswith("fail: used ")


def test_mask_secrets_other_carriers():
    assert "fakebearertoken1234567890" not in rtcfg.mask_secrets("Authorization: Bearer fakebearertoken1234567890")
    assert rtcfg.mask_secrets("clone https://user:fakepass@example.org/x.git") == "clone https://user:***@example.org/x.git"
    assert rtcfg.mask_secrets("password=fakehunter2 ok") == "password=*** ok"
    assert rtcfg.mask_secrets("API_KEY: 'fakekey'") == "API_KEY: ***"
    assert "PRIVATE KEY" not in rtcfg.mask_secrets("-----BEGIN RSA PRIVATE KEY-----")
    assert rtcfg.mask_secrets("nothing to hide: x.py:3: error") == "nothing to hide: x.py:3: error"


def test_first_error_picks_the_line_that_says_what_broke():
    out = "# pkg\nFAIL\n./a.go:3:1: undefined: x\nmore\n"
    assert rtcfg.first_error(out, "") == "./a.go:3:1: undefined: x"
    assert rtcfg.first_error("ok  \tpkg\n---\nplain line\n", "") == "plain line"
    assert rtcfg.first_error("", "") == "no message"
    assert rtcfg.first_error("/tmp/w/src/a.py:1: error: bad", "", strip="/tmp/w/") == "src/a.py:1: error: bad"
    assert len(rtcfg.first_error("x: " + "y" * 900, "", n=50)) == 50


def test_first_error_masks_before_it_cuts():
    out = f"a.py:1: error: token={FAKE['sk']}"
    assert FAKE["sk"] not in rtcfg.first_error(out, "") and "***" in rtcfg.first_error(out, "")
    cut = rtcfg.first_error("x: " + "a" * 20 + FAKE["sk"], "", n=40)  # the cut falls inside the token: no prefix of it survives
    assert FAKE["sk"][:10] not in cut


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def test_the_data_is_shipped_and_complete():
    d = rtcfg.data()
    assert {"limits", "timeouts", "tracers", "result", "copy", "classes", "refresh_days", "keep_runs"} <= set(d)
    assert "target" not in d and "slot_wait_s" not in d["timeouts"] and "java" not in d["tracers"]
    assert set(d["tracers"]) == {"python", "javascript", "go"}
    assert d["tracers"]["python"]["default_command"][:2] == ["python3", "-m"]
    assert set(rtcfg.classes()) >= {"python", "go", "javascript"} and not any(k.startswith("_") for k in rtcfg.classes())
    assert not any(isinstance(v, str) and v.startswith("/") for v in _strings(d)), "no machine-specific path in the data"


def test_the_module_imports_nothing_of_the_checker():
    code = "import sys; sys.path.insert(0, %r); import dead_code_checker.graph.rtcfg as m; print(sorted(k for k in sys.modules if k.startswith('dead_code_checker.graph.')))"
    from pathlib import Path

    root = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-c", code % root], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and eval(r.stdout) == ["dead_code_checker.graph.rtcfg"]


# ------------------------------------------------------------------------------------------------------ the config hook
def test_config_without_the_table_is_off(tmp_path):
    c = config.load(str(tmp_path), environ={}, today=TODAY)
    assert c.runtime == rtcfg.OFF and c.as_dict()["runtime"]["enabled"] is False
    assert c.as_dict()["runtime"]["host"] == "" and c.as_dict()["runtime"]["command"] == []


def test_config_reads_the_table(tmp_path):
    c = mk(tmp_path, 'enabled = true\nhost = "ci-box"\ncommand = ["pytest", "-q"]\ntimeout = 120\nrefresh_days = 2\n')
    assert c.problems == () and c.runtime.enabled and c.runtime.host == "ci-box"
    r = c.as_dict()["runtime"]
    assert r == {"enabled": True, "host": "ci-box", "command": ["pytest", "-q"], "timeout": 120, "refresh_days": 2}
    json.dumps(c.as_dict())


def test_user_and_port_in_the_file_are_one_bad_config_note_and_change_nothing(tmp_path):
    c = mk(tmp_path, 'enabled = true\nhost = "ci-box"\nuser = "root"\nport = 2200\n')
    assert c.runtime == rtcfg.RuntimeCfg(True, "ci-box") and len(c.problems) == 1
    assert c.problems[0].code == "dc.bad-config" and "ignored" in c.problems[0].msg
    assert "user" not in c.as_dict()["runtime"] and "port" not in c.as_dict()["runtime"]


def test_config_problems_are_dc_bad_config_warnings(tmp_path):
    c = mk(tmp_path, 'enabled = true\nhost = "-oProxyCommand=x"\nbogus = 1\ntimeout = 0\n')
    assert c.runtime == rtcfg.OFF
    assert [(p.level, p.code) for p in c.problems] == [("W", "dc.bad-config")] * len(c.problems) and len(c.problems) >= 3
    assert any("bogus" in p.msg for p in c.problems) and any("'host'" in p.msg for p in c.problems)


def test_config_runtime_must_be_a_table(tmp_path):
    (tmp_path / ".dead-code-checker.toml").write_text('[graph]\nruntime = "ci"\n')
    c = config.load(str(tmp_path), environ={}, today=TODAY)
    assert c.runtime == rtcfg.OFF and [p.code for p in c.problems] == ["dc.bad-config"]


def test_the_top_level_rule_still_holds_and_the_tables_own_command_is_data(tmp_path):
    (tmp_path / ".dead-code-checker.toml").write_text('[graph]\ncommand = "x"\n[graph.runtime]\ncommand = "pytest -q"\n')
    c = config.load(str(tmp_path), environ={}, today=TODAY)
    assert [(p.level, p.code) for p in c.problems] == [("E", "dc.bad-config")] and "'command'" in c.problems[0].msg
    assert c.runtime.command == ("pytest", "-q") and not c.runtime.enabled


def test_a_host_of_the_file_is_not_an_allowed_host(tmp_path):
    c = mk(tmp_path, 'enabled = true\nhost = "ci-box"\n')
    assert not rtcfg.host_allowed(c.runtime.host, {}) and rtcfg.host_allowed(c.runtime.host, {rtcfg.ENV_HOSTS: "ci-box"})


# ------------------------------------------------------------------------------------------------------ doctor
def doctor(tmp_path, body, *extra, **env):
    import os

    repo = tmp_path / "app"
    (repo / ".git").mkdir(parents=True)
    if body is not None:
        (repo / ".dead-code-checker.toml").write_text("[graph.runtime]\n" + body)
    e = {k: v for k, v in os.environ.items() if not k.startswith("DEAD_CODE_CHECKER_")}
    e["DEAD_CODE_CHECKER_HOME"] = str(tmp_path / "home")
    e.update(env)
    return subprocess.run(
        [sys.executable, "-P", "-m", "dead_code_checker", "graph", "doctor", "--root", str(repo), *extra],
        capture_output=True, text=True, cwd=tmp_path, env=e, timeout=120,
    )  # fmt: skip


def row(r):
    return next(x for x in r.stdout.splitlines() if x.startswith("runtime:"))


def test_doctor_says_off_by_default_and_contacts_nothing(tmp_path):
    r = doctor(tmp_path, None)
    assert r.returncode == 0, r.stderr
    assert row(r).split(None, 1)[1] == "off"


def test_doctor_names_the_host_and_whether_it_is_allowed(tmp_path):
    body = 'enabled = true\nhost = "ci-box"\n'
    r = doctor(tmp_path, body)
    assert "ci-box" in row(r) and "not allowed" in row(r) and rtcfg.ENV_HOSTS in row(r)
    r = doctor(tmp_path / "b", body, **{rtcfg.ENV_HOSTS: "other, CI-BOX"})
    assert "ci-box" in row(r) and "allowed by" in row(r) and "not allowed" not in row(r)
    r = doctor(tmp_path / "c", "enabled = true\n")
    assert "no host in the file" in row(r)


def test_doctor_json_carries_the_runtime_table_and_the_allowance(tmp_path):
    r = doctor(tmp_path, 'enabled = true\nhost = "ci-box"\n', "--json", **{rtcfg.ENV_HOSTS: "ci-box"})
    doc = json.loads(r.stdout)["config"]
    assert doc["runtime"]["host"] == "ci-box" and "port" not in doc["runtime"] and doc["runtime_host_allowed"] is True


def test_doctor_shows_a_bad_host_as_a_warning_and_the_mode_off(tmp_path):
    r = doctor(tmp_path, 'enabled = true\nhost = "-oProxyCommand=x"\n', "--json")
    doc = json.loads(r.stdout)["config"]
    assert doc["runtime"]["enabled"] is False and doc["runtime"]["host"] == ""
    assert any(p["code"] == "dc.bad-config" and "'host'" in p["msg"] for p in doc["problems"])
