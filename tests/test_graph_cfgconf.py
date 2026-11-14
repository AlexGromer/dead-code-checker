"""graph.extract.cfgconf: HAProxy, keepalived, Grafana Alloy, Redis and LogQL files, read without a grammar.

Every fixture is synthetic text written here (no project content). The extractor is a function of (lang, text); the edges are
checked through the real linker on a small tree, so a path that names a file of the tree becomes an edge and one that does not
is no edge and no finding.

    python3 -m pytest -q -p no:cacheprovider tests/test_graph_cfgconf.py
"""

import random
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("data_root")  # a graph run writes its data into a directory of the test

ROOT = Path(__file__).resolve().parent.parent
from dead_code_checker.graph import contracts, langs as dclangs, link as dlink, model as md, reach as rc  # noqa: E402
from dead_code_checker.graph.extract import batch, cfg, cfgconf  # noqa: E402

LANGS = dclangs.load()
CONF_LANGS = cfgconf.CfgConf.langs

HAPROXY = """\
global
    lua-load /etc/haproxy/lua/auth.lua
    ssl-dh-param-file /etc/haproxy/dh.pem
defaults
    errorfile 503 /etc/haproxy/errors/503.http   # the page
frontend web
    bind :443 ssl crt /etc/haproxy/certs/site.pem ca-file /etc/haproxy/ca.pem
    acl blocked src -f /etc/haproxy/blocked.lst
    http-request set-var(txn.site) req.hdr(host),map(/etc/haproxy/hosts.map)
backend app
    external-check command /usr/local/bin/check-app.sh
    server a 10.0.0.1:80 check ssl crl-file "${CRL_DIR}/c.pem"
    a-directive-of-the-future "never closed
program helper
    command /srv/svc/run-helper.sh --flag
"""

KEEPALIVED = """\
! a comment
global_defs {
    router_id NODE
    notify_email ops@example.invalid
}
include /etc/keepalived/extra/*.conf
vrrp_script chk_app {
    script "/etc/keepalived/check_app.sh --quick"
    interval 2
}
vrrp_script chk_unused { script "pidof nothing" }
vrrp_instance VI_1 {
    state MASTER
    track_script {
        chk_app weight 5
    }
    notify_master "/etc/keepalived/notify.sh master"
    notify /etc/keepalived/on-any.sh
    a_future_block { nested { x } }
}
vrrp_sync_group G { group { VI_1 VI_2 } }
"""

ALLOY = """\
// a comment
logging { level = "info" }
import.file "lib" { filename = "/etc/alloy/lib.alloy" }
import.git "remote" { repository = "https://example.invalid/r.git" path = "a.alloy" }
declare "never_used" {
  argument "targets" {}
  prometheus.scrape "inner" {
    targets    = argument.targets.value
    forward_to = [prometheus.remote_write.out.receiver]
  }
  prometheus.remote_write "out" { endpoint { url = "http://x.invalid" } }
}
lib.shipper "main" { level = "x" }
local.file "token" { filename = "/etc/alloy/token.txt" }
prometheus.scrape "default" {
  targets = [{"__address__" = "a"}]
  forward_to = [
    prometheus.remote_write.mimir.receiver,
  ]
  bearer_token_file = "/run/secrets/t"
}
prometheus.remote_write "mimir" { endpoint { url = "http://m.invalid" } }
"""

REDIS = """\
# a comment
dir /var/lib/redis
dbfilename dump.rdb
include /etc/redis/extra.conf
loadmodule /srv/redis/mod.so ARG
aclfile /etc/redis/users.acl
rename-command CONFIG ""
a-directive-of-the-future a b
sentinel notification-script mymaster /etc/redis/notify.sh
"""


def facts(lang, text):
    return contracts.get_extractor(lang).extract(lang, "x", text)


def refs(f):
    return {(r.name, r.kind, r.scope) for r in f.refs}


def names(f, kind):
    return {r.name for r in f.refs if r.kind == kind}


def quals(f):
    return {d.qual: d.kind for d in f.defs if d.qual}


# ====================================================================== detection
@pytest.mark.parametrize(
    "path,head,lang",
    [
        ("deploy/haproxy.cfg", None, "haproxy"),
        ("deploy/haproxy.conf", None, "haproxy"),
        ("ha/keepalived.conf", None, "keepalived"),
        ("r/redis.conf", None, "redis"),
        ("r/sentinel.conf", None, "redis"),
        ("a/agent.alloy", None, "alloy"),
        ("a/agent.river", None, "alloy"),
        ("q/errors.logql", None, "logql"),
        ("x/lb.cfg", "global\n    daemon\nfrontend f\n    bind :80\n", "haproxy"),
        ("x/lb.conf", "defaults http\n    mode http\n", "haproxy"),
        ("x/vrrp.conf", "vrrp_instance VI_1 {\n    state MASTER\n}\n", "keepalived"),
        ("x/cache-6379.conf", "port 6379\nmaxmemory 1gb\n", "redis"),
        ("x/node.conf", "dbfilename dump.rdb\n", "redis"),
    ],
)
def test_the_files_are_told_by_name_or_by_content_at_the_start_of_a_line(path, head, lang):
    assert LANGS.detect(path, head) == lang


@pytest.mark.parametrize(
    "path,head,lang",
    [
        ("setup.cfg", "[metadata]\nname = x\n", "ini"),
        ("tox.cfg", "[tox]\nglobal = 1\n", "ini"),  # a key named like a section keyword, indented or with a value
        ("a.conf", "# global settings\nkey = value\n", "ini"),
        ("a.conf", "  global\n", "ini"),  # the keywords count at the start of the line
        ("web.conf", "server {\n    listen 80;\n}\n", "nginx"),  # nginx keeps its rule, it comes first
        ("t.cfg", "global {{ a }}\n", "ini"),  # a template is no config
        ("b.conf", "listen on the port\n", "ini"),
    ],
)
def test_content_rules_are_conservative_so_other_ini_files_stay_ini(path, head, lang):
    assert LANGS.detect(path, head) == lang


def test_every_language_is_a_config_language_with_a_registered_extractor():
    for lang in CONF_LANGS:
        assert LANGS.role(lang) == "config" and LANGS.analyzable(lang) and LANGS.grammar(lang) is None
        assert contracts.get_extractor(lang).name == "cfgconf"


# ====================================================================== haproxy
def test_haproxy_directives_tokens_flags_and_converters_give_refs():
    f = facts("haproxy", HAPROXY)
    assert names(f, "include") == {
        "etc/haproxy/dh.pem",
        "etc/haproxy/errors/503.http",
        "etc/haproxy/certs/site.pem",
        "etc/haproxy/ca.pem",
        "etc/haproxy/blocked.lst",
        "etc/haproxy/hosts.map",
        "*/c.pem",  # `${CRL_DIR}/c.pem`: the computed part is a wildcard
    }
    assert names(f, "exec") == {"etc/haproxy/lua/auth.lua", "usr/local/bin/check-app.sh", "srv/svc/run-helper.sh"}
    assert {r.scope for r in f.refs} == {""} and f.errors == 0


def test_haproxy_command_is_a_script_only_in_a_program_section():
    outside = facts("haproxy", "backend b\n    command /srv/x/not-a-script.sh\n")
    assert not outside.refs
    inside = facts("haproxy", "program p\n    command /srv/x/script.sh\n")
    assert names(inside, "exec") == {"srv/x/script.sh"}


def test_haproxy_file_is_a_soft_root_and_unknown_directives_are_skipped():
    f = facts(
        "haproxy", "global\n    some-new-keyword 1 2 3\n    # lua-load /commented.lua\nfrontend f\n    bind :80\n"
    )
    assert not f.refs and [(r.kind, r.target, r.strength) for r in f.roots] == [("init", "", "soft")]


# ====================================================================== keepalived
def test_keepalived_defs_refs_scripts_and_includes():
    f = facts("keepalived", KEEPALIVED)
    assert quals(f) == {
        "vrrp_script:chk_app": "job",
        "vrrp_script:chk_unused": "job",
        "vrrp_instance:VI_1": "resource",
        "vrrp_sync_group:G": "resource",
    }
    got = refs(f)
    assert ("vrrp_script:chk_app", "ref", "vrrp_instance:VI_1") in got  # track_script { chk_app weight 5 }
    assert ("vrrp_instance:VI_2", "ref", "vrrp_sync_group:G") in got and (
        "vrrp_instance:VI_1",
        "ref",
        "vrrp_sync_group:G",
    ) in got
    assert ("vrrp_instance:VI_1", "ref", "") in got and ("vrrp_sync_group:G", "ref", "") in got  # started with the file
    assert ("etc/keepalived/check_app.sh", "exec", "vrrp_script:chk_app") in got
    assert ("etc/keepalived/notify.sh", "exec", "vrrp_instance:VI_1") in got
    assert ("etc/keepalived/on-any.sh", "exec", "vrrp_instance:VI_1") in got  # an unquoted command
    assert ("etc/keepalived/extra/*.conf", "include", "") in got
    assert not [r for r in got if "ops@" in r[0] or "pidof" in r[0]]  # an address and a bare command are no files
    assert f.errors == 0


def test_keepalived_untracked_script_is_dead_and_the_tracked_one_lives():
    v = _verdicts({"ha/keepalived.conf": KEEPALIVED})  # a soft root of the file: weak-alive is alive, never dead
    live = ("alive", "weak-alive")
    assert v["vrrp_script:chk_app"] in live and v["vrrp_script:chk_unused"] == "dead"
    assert v["vrrp_instance:VI_1"] in live and v["vrrp_sync_group:G"] in live


def test_keepalived_unbalanced_braces_are_counted_not_raised():
    assert facts("keepalived", "vrrp_instance V {\n  state MASTER\n").errors == 1
    assert facts("keepalived", "}\n}\n").errors == 2


# ====================================================================== alloy
def test_alloy_components_declares_imports_and_paths():
    f = facts("alloy", ALLOY)
    q = quals(f)
    assert q["declare:never_used"] == "resource"
    assert q["component:prometheus%2Escrape%2Edefault"] == "resource"
    assert q["component:never_used%3Aprometheus%2Escrape%2Einner"] == "resource"  # inside a declare: prefixed with it
    got = refs(f)
    scrape = "component:prometheus%2Escrape%2Edefault"
    assert ("component:prometheus%2Eremote_write%2Emimir", "ref", scrape) in got  # forward_to = [a.b.label.receiver]
    assert (
        "component:never_used%3Aprometheus%2Eremote_write%2Eout",
        "ref",
        "component:never_used%3Aprometheus%2Escrape%2Einner",
    ) in got
    assert (scrape, "ref", "") in got  # the runtime starts every top-level component
    assert ("declare:shipper", "ref", "") in got  # lib.shipper: the declare `shipper` of the file imported as `lib`
    assert names(f, "include") == {"etc/alloy/lib.alloy", "etc/alloy/token.txt", "run/secrets/t"}
    assert not [r for r in got if "example.invalid" in r[0]]  # import.git is remote: no edge
    assert f.errors == 0


def test_alloy_unused_declare_is_dead_and_an_instance_keeps_it_alive():
    dead = _verdicts({"a/main.alloy": 'declare "d" {\n  local.file "f" { filename = "x" }\n}\n'})
    gone = ("dead", "dead-cluster")
    assert dead["declare:d"] in gone and dead["component:d%3Alocal%2Efile%2Ef"] in gone
    live = _verdicts({"a/main.alloy": 'declare "d" {\n  local.file "f" { filename = "x" }\n}\nd "one" { }\n'})
    assert live["declare:d"] in ("alive", "weak-alive") and live["component:d%3Alocal%2Efile%2Ef"] in (
        "alive",
        "weak-alive",
    )


def test_alloy_declare_of_an_imported_file_is_found_across_files_by_name():
    files = {
        "a/main.alloy": 'import.file "lib" { filename = "a/lib.alloy" }\nlib.shipper "main" { }\n',
        "a/lib.alloy": 'declare "shipper" { }\ndeclare "spare" { }\n',
    }
    v = _verdicts(files)
    assert v["declare:shipper"] in ("alive", "weak-alive") and v["declare:spare"] == "dead"


def test_alloy_comments_and_strings_hide_names_and_braces():
    f = facts("alloy", '/* prometheus.scrape "no" {\n} */\nlocal.file "a" { filename = "a}{//b.txt" }\n')
    assert set(quals(f)) == {"component:local%2Efile%2Ea"} and f.errors == 0
    assert names(f, "include") == {
        "a}{//b.txt"
    }  # a string is a string: braces and slashes in it open and comment nothing


def test_alloy_unbalanced_braces_are_counted():
    assert facts("alloy", 'local.file "a" {\n').errors == 1
    assert facts("alloy", "}\n").errors == 1


# ====================================================================== redis
def test_redis_directives_paths_and_scripts():
    f = facts("redis", REDIS)
    assert names(f, "include") == {
        "etc/redis/extra.conf",
        "srv/redis/mod.so",
        "etc/redis/users.acl",
        "dump.rdb",
        "var/lib/redis/dump.rdb",  # a relative name is also relative to `dir`
    }
    assert names(f, "exec") == {"etc/redis/notify.sh"}
    assert [(r.kind, r.strength) for r in f.roots] == [("init", "soft")]


def test_redis_comments_and_rename_command_name_nothing():
    f = facts("redis", '# include /etc/redis/x.conf\nrename-command CONFIG ""\nrename-command FLUSHALL /not/a/file\n')
    assert not f.refs


def test_redis_glob_include_is_a_pattern():
    assert names(facts("redis", "include /etc/redis/conf.d/*.conf\n"), "include") == {"etc/redis/conf.d/*.conf"}


# ====================================================================== logql and the Loki ruler
def test_logql_file_is_a_soft_root_and_names_nothing():
    f = facts("logql", '{app="x"} |= "error" | json\n')
    assert not f.refs and [(r.kind, r.strength) for r in f.roots] == [("init", "soft")]


def test_loki_config_names_the_rule_directory_and_rule_files_are_soft_roots():
    ruler = "ruler:\n  storage:\n    type: local\n    local:\n      directory: /loki/rules\n  rule_path: /tmp/rules\n"
    f = facts("yaml", ruler)
    assert names(f, "include") == {"loki/rules/**"}  # rule_path is a scratch directory
    rules = (
        'groups:\n  - name: errors\n    rules:\n      - alert: Many\n        expr: sum(rate({a="b"} |= "e" [5m])) > 1\n'
    )
    g = facts("yaml", rules)
    assert [(r.kind, r.strength) for r in g.roots] == [("init", "soft")]


# ====================================================================== the plan's acceptance test
def _extract(path, text):
    lang = LANGS.detect(path, text[:4096]) or "yaml"
    return contracts.get_extractor(lang).extract(lang, path, text) if lang in (*CONF_LANGS, "yaml") else md.Facts(lang)


def _graph(files):
    fs = {p: _extract(p, t) for p, t in files.items()}
    res = dlink.link(fs, {}, workers=1)
    assert not res.findings, res.findings
    return res


def _edges(files):
    """{(source file or qual, target file, kind, tier)} of the file-to-file edges (targets without a `#`)."""
    res = _graph(files)
    return {
        (e.src.split(":", 1)[1], e.dst.split(":", 1)[1], e.kind, e.tier) for e in res.graph.edges if "#" not in e.dst
    }


def _verdicts(files):
    res = _graph(files)
    return {k.split("#", 1)[1]: v for k, v in rc.analyze(res.graph).verdicts().items() if "#" in k}


TREE = {
    "lb/haproxy.cfg": HAPROXY,
    "lb/etc/haproxy/lua/auth.lua": "x = 1\n",
    "lb/etc/haproxy/errors/503.http": "HTTP/1.0 503\n",
    "lb/etc/haproxy/certs/site.pem": "pem\n",
    "lb/usr/local/bin/check-app.sh": "#!/bin/sh\n",
    "ha/keepalived.conf": KEEPALIVED,
    "ha/etc/keepalived/check_app.sh": "#!/bin/sh\n",
    "ha/etc/keepalived/notify.sh": "#!/bin/sh\n",
    "ha/etc/keepalived/extra/a.conf": "vrrp_instance X {\n}\n",
    "obs/agent.alloy": ALLOY,
    "obs/etc/alloy/lib.alloy": 'declare "shipper" { }\n',
    "obs/etc/alloy/token.txt": "t\n",
    "cache/redis.conf": REDIS,
    "cache/etc/redis/extra.conf": "maxmemory 1gb\n",
    "cache/etc/redis/notify.sh": "#!/bin/sh\n",
    "cache/srv/redis/mod.so": "bin\n",
}


def test_include_edges():
    """A HAProxy, keepalived, Alloy or Redis file that names a script or an include has an edge to that file (acceptance)."""
    e = _edges(TREE)
    def has(src, dst, kind):
        return any(
            s.endswith(src) and d.endswith(dst) and k == kind for s, d, k, _ in e
        )  # noqa: E731
    assert has("lb/haproxy.cfg", "lb/etc/haproxy/lua/auth.lua", "exec")
    assert has("lb/haproxy.cfg", "lb/usr/local/bin/check-app.sh", "exec")
    assert has("lb/haproxy.cfg", "lb/etc/haproxy/errors/503.http", "include")
    assert has("lb/haproxy.cfg", "lb/etc/haproxy/certs/site.pem", "include")
    assert has("vrrp_script:chk_app", "ha/etc/keepalived/check_app.sh", "exec")
    assert has("vrrp_instance:VI_1", "ha/etc/keepalived/notify.sh", "exec")
    assert has("ha/keepalived.conf", "ha/etc/keepalived/extra/a.conf", "include")  # a glob include
    assert has("obs/agent.alloy", "obs/etc/alloy/lib.alloy", "include")
    assert has("component:local%2Efile%2Etoken", "obs/etc/alloy/token.txt", "include")
    assert has("cache/redis.conf", "cache/etc/redis/extra.conf", "include")
    assert has("cache/redis.conf", "cache/srv/redis/mod.so", "include")
    assert has("cache/redis.conf", "cache/etc/redis/notify.sh", "exec")


def test_a_name_that_one_file_of_the_tree_has_is_a_t_edge_and_several_files_make_it_n():
    one = _edges({"a/redis.conf": "include /etc/r/x.conf\n", "a/etc/r/x.conf": "maxmemory 1\n"})
    assert {(k, t) for s, d, k, t in one if d.endswith("etc/r/x.conf")} == {("include", "T")}
    two = _edges({"a/redis.conf": "include x.conf\n", "a/x.conf": "maxmemory 1\n", "b/x.conf": "maxmemory 2\n"})
    assert {t for s, d, k, t in two if d.endswith("x.conf")} == {"N"}


def test_a_missing_referenced_file_is_no_edge_no_finding_and_no_crash():
    files = {
        "lb/haproxy.cfg": "global\n    lua-load /etc/haproxy/gone.lua\n",
        "cache/redis.conf": "include /etc/redis/gone.conf\n",
    }
    res = _graph(files)  # asserts: no finding
    assert not [e for e in res.graph.edges if e.kind in ("exec", "include")]


# ====================================================================== robustness and determinism
@pytest.mark.parametrize("lang", CONF_LANGS)
def test_hostile_input_never_raises_and_is_deterministic(lang):
    rng = random.Random(7)
    alphabet = list("{}[]()=\"'`#!/\\*.,;:$%_ \t\n") + [
        "include ",
        "script ",
        "import.file ",
        "vrrp_script ",
        "crt ",
        "-f ",
        "map(",
    ]
    samples = ["", "\n", "﻿", "{", "}", '"', "\\", "include", "import.file", "a = ", 'a "', "/*", "`"]
    samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 200))) for _ in range(60)]
    for text in samples:
        a, b = facts(lang, text), facts(lang, text)
        assert a.to_payload() == b.to_payload()


def test_batch_extract_is_the_same_for_1_and_2_workers():
    items = [("haproxy", "h", "%040x" % 1, HAPROXY), ("keepalived", "k", "%040x" % 2, KEEPALIVED)]
    items += [
        ("alloy", "a", "%040x" % 3, ALLOY),
        ("redis", "r", "%040x" % 4, REDIS),
        ("logql", "q", "%040x" % 5, '{a="b"}\n'),
    ]
    one, miss1 = batch.extract_many(items, workers=1)
    two, miss2 = batch.extract_many(items, workers=2, chunk=2)
    assert not miss1 and not miss2 and [s.key for s in one] == [s.key for s in two]
    assert [s.facts().to_payload() for s in one] == [s.facts().to_payload() for s in two]


def test_the_shard_key_follows_the_rules_and_the_module():
    ex = contracts.get_extractor("redis")
    assert ex.grammar_sha("redis") == ex.grammar_sha("haproxy") and len(ex.grammar_sha("redis")) == 64
    assert cfg.rules()["loki"]["rule_dirs"]  # the Loki rule is data of cfg.json
