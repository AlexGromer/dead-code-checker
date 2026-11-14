"""The root extractors of the JavaScript world and the web: package.json, html pages, WebExtension manifests, tool configs,
single-file components. They read one file's text and register entry points; the tests look at the hints and at how they resolve
against a tree.

python3 -m pytest -q tests/test_graph_roots_js.py
"""

import json

import pytest

from dead_code_checker.graph import roots


PKG = """{
  "name": "p",
  "main": "lib/index.js",
  "module": "./esm/index.mjs",
  "bin": {"tool": "bin/tool.js", "other": "./bin/other.js"},
  "exports": {".": {"import": "./esm/index.mjs", "require": "./cjs/index.cjs"}, "./sub/*": "./s/*.js", "./package.json": "./package.json"},
  "browser": {"fs": false},
  "scripts": {"start": "node server.js"}
}
"""


MANIFEST = json.dumps(
    {
        "manifest_version": 3,
        "background": {"service_worker": "dist/background.js"},
        "content_scripts": [
            {"matches": ["<all_urls>"], "js": ["content/a.js", "/content/b.js"], "css": ["x.css"]},
            {"js": ["c.js"]},
        ],
        "action": {"default_popup": "popup.html"},
    }
)


PAGE = (
    '<html>\n<body onload="init()">\n<button onclick="onSave()">save</button>\n'
    '<script src="app.js"></script>\n<script type="module" src="/src/main.js"></script>\n<script>init()</script>\n</body></html>\n'
)


VUE = '<template><Btn /></template>\n<script setup lang="ts">\nimport Btn from "./components/Btn.vue";\nimport { fmt } from "@/utils/format";\nimport Vue from "vue";\nconst m = await import("../lazy");\n</script>\n'


def _entry_roots(pkg: str, tree):
    hints = {"package.json": roots.extract_hints("package.json", pkg)}
    rs = roots.resolve(hints, {p: "100644" for p in ["package.json", *tree]}, lambda p: "")
    return {r.node.split(":", 1)[1] for r in rs.roots if r.kind == "export"}, rs


def test_built_entry_maps_to_js_sources_and_to_dual_build_layouts():
    """Counterexamples of the review: `./lib/index.js` gave only src/index.ts[x]; `./dist/esm/index.js` gave src/esm/index.ts."""
    js = ["src/index.js", "src/index.jsx", "src/index.mjs", "src/other.js"]
    got, rs = _entry_roots('{"main": "./lib/index.js"}', js)
    assert got == {"src/index.js", "src/index.jsx", "src/index.mjs"} and not rs.findings
    tree = ["src/index.ts", "src/util/deep.ts", "src/other.ts"]
    pkg = '{"main": "./dist/cjs/index.js", "module": "./dist/esm/index.js", "types": "./dist/types/index.d.ts"}'
    assert _entry_roots(pkg, tree)[0] == {"src/index.ts"}  # dist/esm and dist/cjs name no directory of ours
    assert _entry_roots('{"exports": {"./util/*": "./dist/esm/util/*.js"}}', tree)[0] == {"src/util/deep.ts"}
    assert _entry_roots('{"main": "./dist/src/index.js"}', tree)[0] == {"src/index.ts"}  # tsc with rootDir "."
    assert _entry_roots('{"main": "./build/index.cjs"}', ["source/index.cts"])[0] == {"source/index.cts"}
    assert _entry_roots('{"main": "./index.js"}', ["index.ts", "src/index.ts"])[0] == {"index.ts"}  # built in place
    assert (
        _entry_roots('{"main": "./dist/index.js"}', ["lib/index.ts", "tests/index.ts"])[0] == set()
    )  # not a source dir


def test_npm_roots_main_bin_exports_conditions():
    h = {(x.kind, x.target, x.line) for x in roots.extract_hints("pkg/package.json", PKG)}
    plain = {
        (k, t.removeprefix("sibling:"), ln)
        for k, t, ln in h
        if not t.startswith(("sibling:./lib/", "sibling:./esm/", "sibling:./cjs/", "sibling:./s/"))
        or t.endswith((".js", ".mjs", ".cjs"))
    }  # noqa: E501
    assert {
        ("export", "./lib/index.js", 3),
        ("export", "./esm/index.mjs", 4),
        ("export", "./cjs/index.cjs", 6),
        ("export", "./s/*.js", 6),  # a subpath pattern: every file it matches (resolve)
        ("export", "./server.js", 8),  # scripts: `node server.js`
        ("export", "./package.json", 6),
        ("bin", "./bin/tool.js", 5),
        ("bin", "./bin/other.js", 5),
    } <= plain
    # main, module and exports name build outputs: the sources they are built from are hints too (silent when absent)
    src = {t.removeprefix("sibling:./src/index") for _, t, _ in h if t.startswith("sibling:./src/index")}
    assert src == {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
    assert ("export", "sibling:./s/*.js", 6) in h and ("export", "sibling:./src/*.ts", 6) not in h  # ./s is no out dir
    assert all(x.trust == "registered" for x in roots.extract_hints("package.json", PKG))
    assert "sibling:./cli.js" in {h.target for h in roots.extract_hints("package.json", '{"bin": "cli.js"}')}
    # a package.json that is no JSON object is a bad source (W dc.bad-root-source at resolve), not silence and not a crash
    for broken in ("{not json", "[]"):
        assert [h.kind for h in roots.extract_hints("package.json", broken)] == ["bad-source"]
    assert (
        roots.extract_hints("lib/package-lock.json", PKG) == [] and roots.extract_hints("a/package.json.bak", PKG) == []
    )


def test_npm_roots_resolve_against_the_tree_and_a_missing_build_output_is_silent():
    hints = {"pkg/package.json": roots.extract_hints("pkg/package.json", PKG)}
    files = {
        "pkg/package.json": "100644",
        "pkg/lib/index.js": "100644",
        "pkg/bin/tool.js": "100755",
        "pkg/esm/index.mjs": "100644",
    }
    rs = roots.resolve(hints, files, lambda p: "")
    got = {(r.kind, r.node) for r in rs.roots if r.kind in ("export", "bin")}
    assert ("export", "javascript:pkg/lib/index.js") in got and ("bin", "javascript:pkg/bin/tool.js") in got
    assert ("export", "javascript:pkg/esm/index.mjs") in got
    assert not [
        f for f in rs.findings if f.code == "dc.root-dangling"
    ], "dist/ is built: a missing file is no finding"  # cjs/ and other.js are absent


def test_npm_roots_types_source_scripts_and_dist_to_src():
    pkg = '{"main": "dist/index.js", "types": "./dist/index.d.ts", "source": "src/main.ts", "scripts": {"a": "tsx src/cli.ts", "b": "echo hi", "c": "node /abs/x.js"}}'
    h = {(x.target, x.strength) for x in roots.extract_hints("package.json", pkg)}
    assert ("sibling:./src/index.ts", "hard") in h and ("sibling:./src/main.ts", "hard") in h
    assert ("sibling:./src/cli.ts", "soft") in h and not [t for t, _ in h if "abs" in t or "hi" in t]
    files = {"package.json": "100644", "src/index.ts": "100644", "src/lib.ts": "100644"}
    rs = roots.resolve({"package.json": roots.extract_hints("package.json", pkg)}, files, lambda p: "")
    assert {r.node for r in rs.roots if r.kind == "export"} == {"typescript:src/index.ts"} and not rs.findings


def test_framework_entry_conventions_are_data_driven_and_need_the_dependency():
    nxt = '{"dependencies": {"next": "14", "react": "18"}}'
    hs = {(h.target, h.trust, h.strength) for h in roots.extract_hints("web/package.json", nxt)}
    assert ("sibling:./pages/*", "convention", "hard") in hs and ("sibling:./src/app/*", "convention", "hard") in hs
    assert not [t for t, _, _ in hs if "main" in t]  # vite is not a dependency
    assert roots.extract_hints("package.json", '{"dependencies": {"react": "18"}}') == []
    tree = ["web/package.json", "web/pages/index.tsx", "web/pages/api/x.ts", "web/lib/util.ts", "web/pages/style.css"]
    rs = roots.resolve(
        {"web/package.json": roots.extract_hints("web/package.json", nxt)}, {p: "100644" for p in tree}, lambda p: ""
    )
    assert {r.node for r in rs.roots if r.kind == "export"} == {
        "tsx:web/pages/index.tsx",
        "typescript:web/pages/api/x.ts",
    }
    vite = '{"devDependencies": {"vite": "5"}}'
    assert ("sibling:./src/main.*", "convention") in {
        (h.target, h.trust) for h in roots.extract_hints("package.json", vite)
    }
    assert "js_frameworks" in roots.fw() and {"next", "vite", "webpack", "@angular/core"} <= set(
        roots.fw()["js_frameworks"]
    )


def test_index_html_local_script_is_an_entry_remote_ones_are_not():
    html = '<html>\n<script src="https://cdn.x/y.js"></script>\n<script type="module" src="/src/main.tsx"></script>\n<script src=\'./boot.js?v=1\'></script>\n'
    hs = [(h.kind, h.target, h.line) for h in roots.extract_hints("app/index.html", html)]
    # a module script is an entry (its API); a classic one is kind script: its top-level definitions are globals of the page
    assert hs == [("export", "sibling:./src/main.tsx", 3), ("script", "sibling:./boot.js", 4)]
    assert roots.extract_hints("app/popup.html", html) == roots.extract_hints(
        "app/index.html", html
    )  # any page, not index.html only
    assert roots.extract_hints("app/page.txt", html) == []
    rs = roots.resolve(
        {"app/index.html": roots.extract_hints("app/index.html", html)}, {"app/src/main.tsx": "100644"}, lambda p: ""
    )
    assert [r.node for r in rs.roots] == ["tsx:app/src/main.tsx"] and not rs.findings


def test_an_extensionless_bin_is_a_root():
    pkg = '{"bin": {"tool": "./bin/tool"}}'
    hints = {"package.json": roots.extract_hints("package.json", pkg)}
    rs = roots.resolve(hints, {"package.json": "100644", "bin/tool.js": "100644"}, lambda p: "")
    assert {r.node for r in rs.roots if r.kind == "bin"} == {"javascript:bin/tool.js"}


def test_bare_script_words_are_files_only_for_an_interpreter():
    assert roots.command_targets("node server") == ["cmd:server"]  # the shared parser is unchanged
    assert roots.command_targets("node server", bare=True) == ["file:server"]
    assert roots.command_targets("eslint .", bare=True) == ["cmd:eslint"]  # a plain command stays a command
    assert roots.command_targets("node ./server --port 3", bare=True) == ["file:server"]
    assert roots.command_targets("tsx watch src/a.ts", bare=True) == ["cmd:watch"]  # a subcommand is no file
    assert sorted(roots._script_files("tsx watch src/a.ts && node b.mjs")) == ["b.mjs", "src/a.ts"]
    assert roots._script_files("echo hi && jest --coverage") == []


@pytest.mark.parametrize(
    "config, text, tree, want",
    [
        (
            "webpack.config.js",
            "module.exports = { entry: './src/app.js', mode: 'production' };\n",
            ["src/app.js", "src/x.js"],
            {"src/app.js"},
        ),
        (
            "webpack.config.js",
            "module.exports = {\n  entry: {\n    main: './src/main.js',\n    admin: ['./src/admin.js', './src/polyfills.js'],\n  },\n};\n",
            ["src/main.js", "src/admin.js", "src/polyfills.js", "src/x.js"],
            {"src/main.js", "src/admin.js", "src/polyfills.js"},
        ),
        (
            "rollup.config.js",
            "export default { input: 'src/main.js', output: { file: 'dist/b.js' } };\n",
            ["src/main.js", "dist/b.js"],
            {"src/main.js"},
        ),
        (
            "tsup.config.ts",
            "export default defineConfig({ entry: ['src/index.ts', 'src/cli.ts'], format: ['esm'] });\n",
            ["src/index.ts", "src/cli.ts", "src/x.ts"],
            {"src/index.ts", "src/cli.ts"},
        ),
        (
            "scripts/esbuild.config.mjs",
            "await build({ entryPoints: [path.resolve(__dirname, 'app/main.ts')] });\n",
            ["scripts/app/main.ts", "app/main.ts"],
            {"scripts/app/main.ts"},
        ),
        (
            "webpack.config.js",
            "module.exports = { entry: './src/index' };\n",
            ["src/index.jsx"],
            {"src/index.jsx"},
        ),  # extensionless
        (
            "vite.config.ts",
            "export default { build: { lib: { entry: resolve(__dirname, 'lib/main.ts') } } };\n",
            ["lib/main.ts"],
            {"lib/main.ts"},
        ),
    ],
    ids=["webpack-single", "webpack-multi", "rollup", "tsup", "esbuild", "webpack-extless", "vite-lib"],
)
def test_bundler_config_entries_are_roots(config, text, tree, want):
    """Counterexample of the review: a webpack `entry`, a rollup `input`, a tsup / esbuild `entry` gave no root."""
    hints = {config: roots.extract_hints(config, text)}
    assert {h.kind for h in hints[config]} == {
        "main",
        "export",
    }  # the config is an entry itself, its bundle entries export
    rs = roots.resolve(hints, {p: "100644" for p in [config, *tree]}, lambda p: "")
    assert {r.node.split(":", 1)[1] for r in rs.roots if r.kind == "export"} == want and not rs.findings


def test_a_config_value_that_is_no_path_is_no_entry():
    text = "module.exports = { entry: 'main', input: 'text', entries: [url + '/x'], entryPoints: ['https://cdn/x.js', '@scope/pkg'] };\n"
    assert [h.kind for h in roots.extract_hints("webpack.config.js", text)] == ["main"]


def test_files_imported_only_from_single_file_components_are_entries():
    """Counterexample of the review: .vue / .svelte / .astro are no language, so what only their script imports had no importer."""
    got = {(h.kind, h.target, h.line, h.strength) for h in roots.extract_hints("src/App.vue", VUE)}
    assert not [t for _, t, _, _ in got if "Btn" in t]  # a component is an entry itself, not code of the graph
    assert ("export", "file:src/utils/format", 4, "hard") in got and (
        "export",
        "file:src/utils/format.ts",
        4,
        "hard",
    ) in got
    assert ("export", "file:src/utils/format/index.tsx", 4, "hard") in got
    assert ("export", "sibling:./../lazy.ts", 6, "hard") in got
    assert not [t for _, t, _, _ in got if "vue" in t]  # a package ("vue") is no file of ours
    svelte = "<script>\n  import { db } from '$lib/db';\n  import './app.css';\n</script>\n<h1>x</h1>\n"
    assert {
        (h.target, h.line)
        for h in roots.extract_hints("src/routes/+page.svelte", svelte)
        if h.target.startswith("file:")
    } >= {("file:src/lib/db.js", 2)}
    assert not [h for h in roots.extract_hints("src/routes/+page.svelte", svelte) if "css" in h.target]  # no code
    astro = "---\nimport Layout from '../layouts/Main';\nconst x = 1;\n---\n<Layout />\n<script>\n  import '../client';\n</script>\n"
    ts = {h.target for h in roots.extract_hints("src/pages/index.astro", astro)}
    assert "sibling:./../layouts/Main.ts" in ts and "sibling:./../client.js" in ts
    for other in ("src/a.js", "src/a.vue.bak", "src/a.html"):
        assert roots.extract_hints(other, VUE) == [], other


def test_a_script_root_is_hard_and_seeds_top_level_definitions_only():
    rs = roots.resolve(
        {"index.html": roots.extract_hints("index.html", PAGE)},
        {p: "100644" for p in ("app.js", "src/main.js")},
        lambda p: "",
    )
    sc = [r for r in rs.roots if r.kind == "script"]
    assert [(r.node, r.strength, r.trust) for r in sc] == [("javascript:app.js", "hard", "registered")]


def test_what_an_inline_module_script_imports_is_an_entry_of_the_page():
    html = '<script type="module">\n  import { init } from "./app.js";\n  import("@/lazy");\n  init();\n</script>\n<script>import("./classic.js")</script>\n'
    hs = {(h.kind, h.target, h.line) for h in roots.extract_hints("web/index.html", html)}
    assert ("export", "sibling:./app.js", 2) in hs and ("export", "file:src/lazy.ts", 3) in hs
    assert not [h for h in hs if "classic" in h[1]]  # a classic inline script has no import statement


def test_script_tags_after_a_script_without_a_closing_tag_are_all_read():
    html = '<script src="a.js"/>\n<script src="b.js"></script>\n<script src=c.js defer></script>\n<script src="//cdn/x.js"></script>\n'
    got = [(h.kind, h.target) for h in roots.extract_hints("index.html", html)]
    assert got == [("script", "sibling:./a.js"), ("script", "sibling:./b.js"), ("script", "sibling:./c.js")]


def test_a_vuepress_config_is_a_tool_config_without_the_dependency():
    assert {h.kind for h in roots.extract_hints(".vuepress/config.js", "module.exports = {}")} == {"main"}
    assert {h.kind for h in roots.extract_hints("docs/.vuepress/enhanceApp.ts", "export default {}")} == {"main"}
    assert roots.extract_hints("docs/.vuepress/other.js", "x") == []


def test_webextension_manifest_scripts_are_entries_and_a_built_one_maps_to_its_source():
    got = {(h.kind, h.target) for h in roots.extract_hints("ext/manifest.json", MANIFEST)}
    assert {("export", "sibling:./dist/background.js"), ("export", "sibling:./src/background.ts")} <= got
    assert {
        ("export", "sibling:./content/a.js"),
        ("export", "sibling:./content/b.js"),
        ("export", "sibling:./c.js"),
    } <= got
    assert not [t for _, t in got if "css" in t or "popup" in t]  # a page is read through html_files
    assert roots.extract_hints("manifest.json", '{"name": "a pwa", "start_url": "/"}') == []  # a web app manifest
    assert roots.extract_hints("manifest.json", "not json") == []
    tree = ["ext/manifest.json", "ext/src/background.ts", "ext/content/a.js", "ext/c.js", "ext/lib.js"]
    rs = roots.resolve(
        {"ext/manifest.json": roots.extract_hints("ext/manifest.json", MANIFEST)},
        {p: "100644" for p in tree},
        lambda p: "",
    )
    assert {r.node.split(":", 1)[1] for r in rs.roots} == {"ext/src/background.ts", "ext/content/a.js", "ext/c.js"}
    assert not rs.findings  # dist/background.js and content/b.js are not in the tree: silent


# ---------------------------------------------------------------------- entries without an extension, directories, scripts
@pytest.mark.parametrize(
    "pkg, tree, want",
    [
        ('{"main": "index"}', ["index.js", "other.js"], {"index.js"}),
        ('{"main": "lib"}', ["lib/index.js", "lib/util.js"], {"lib/index.js"}),
        ('{"main": "./lib/"}', ["lib/index.js", "lib/util.js"], {"lib/index.js"}),
        ('{"main": "lib"}', ["lib.js", "lib/util.js"], {"lib.js"}),
        ('{"main": "./lib/"}', ["src/index.ts", "src/util.ts"], {"src/index.ts"}),  # built into lib/
        ('{"main": "index"}', ["index.mjs"], {"index.mjs"}),
        ('{"main": "."}', ["index.js", "util.js"], {"index.js"}),
        ('{"main": "./"}', ["index.ts", "util.ts"], {"index.ts"}),
        ('{"scripts": {"start": "node server"}}', ["server.js", "lib.js"], {"server.js"}),
        ('{"scripts": {"start": "node ./server --port 3"}}', ["server.js"], {"server.js"}),
        ('{"scripts": {"start": "node dist/app"}}', ["src/app.ts"], {"src/app.ts"}),
        (
            '{"scripts": {"build": "tsup src/index.ts src/cli.ts --format esm"}}',
            ["src/index.ts", "src/cli.ts", "src/x.ts"],
            {"src/index.ts", "src/cli.ts"},
        ),
        ('{"scripts": {"build": "esbuild --entry=./app/main.js --bundle"}}', ["app/main.js"], {"app/main.js"}),
        ('{"scripts": {"dev": "cross-env X=1 node -r dotenv/config server.js"}}', ["server.js"], {"server.js"}),
    ],
)
def test_extensionless_and_directory_entries_and_script_files_are_roots(pkg, tree, want):
    """Counterexamples of the review: `"main": "index"`, `"main": "lib"`, `"main": "./lib/"` and `node server` gave no root."""
    got, rs = _entry_roots(pkg, tree)
    assert got == want and not rs.findings


def test_npm_source_candidates_are_data():
    b = roots.fw()["js_build"]
    assert {"dist", "lib", "esm"} <= set(b["out_dirs"]) and {"esm", "cjs", "types"} <= set(b["variant_dirs"])
    assert b["src_dirs"] and {".ts", ".tsx", ".js", ".jsx", ".mjs"} <= set(b["src_exts"])
    assert (
        roots._npm_sources("./dist/esm/index.d.ts")[-len(b["src_dirs"]) * len(b["src_exts"]) :][0]
        == f"./{b['src_dirs'][0]}/index{b['src_exts'][0]}"
    )
    assert all(
        c != "./lib/index.js" for c in roots._npm_sources("./lib/index.js")
    )  # the built path itself is not a candidate
