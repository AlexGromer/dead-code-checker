"""graph.extract: one module per extractor family, named in data/langs.json ("extractor": "<module>").

A module defines Extractor subclasses (graph.contracts) and registers instances at import with contracts.register_extractor();
contracts.get_extractor(lang) imports the module on first use.

    py          Python (stdlib ast)
    ts_generic  every language that data/queries/<grammar>/*.scm describes: the tree-sitter matches become Facts (ts_lang, lang_rules
                and data/rules.json hold the per-language rules)
    cfg, cfgts, cfgconf, tpl     config languages: YAML (k8s, Helm, CI, compose, Ansible) and HCL; make, just, dockerfile, nginx, jsonnet;
                HAProxy, keepalived, Alloy, Redis, LogQL by line scanners; Jinja2 templates
    schema      SQL, protobuf and GraphQL objects
    pwsh        PowerShell, parsed by pwsh itself
    xlang       the cross-language facts (exec, lit, ffi, env, schema) that the others write; yamlite: the YAML reader; common: what they share
    batch       extract many files in parallel

The tree-sitter ones need the optional extra (graph.ts); an extractor that cannot run says so through Extractor.probe, and a language
whose module is absent is reported as not supported (dc.unsupported-language), never skipped in silence.
"""
