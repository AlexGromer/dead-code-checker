#!/usr/bin/env python3
"""Turn the JSON of `dead-code-checker graph check --format json` into a shields.io endpoint badge.

    python3 scripts/badge.py --findings dcc.json --out dead-code-badge.json

Writes the endpoint JSON (https://shields.io/badges/endpoint-badge, schemaVersion 1) and prints `errors=N` and
`warnings=N` lines, which the composite action appends to $GITHUB_OUTPUT. Standard library only.

    no findings   "dead code: none"         brightgreen
    warnings      "dead code: N warnings"   yellow
    errors        "dead code: N errors"     red
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence


def badge(errors: int, warnings: int, label: str = "dead code") -> dict:
    if errors:
        msg, color = f"{errors} error{'s' if errors != 1 else ''}", "red"
    elif warnings:
        msg, color = f"{warnings} warning{'s' if warnings != 1 else ''}", "yellow"
    else:
        msg, color = "none", "brightgreen"
    return {"schemaVersion": 1, "label": label, "message": msg, "color": color}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="badge.py", description=__doc__.split("\n")[0])
    ap.add_argument("--findings", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="dead code")
    a = ap.parse_args(argv)
    try:
        with open(a.findings, encoding="utf-8") as fh:
            doc = json.load(fh)
        fs = doc.get("findings", []) if isinstance(doc, dict) else doc
        errors = sum(1 for f in fs if isinstance(f, dict) and f.get("level") == "E")
        warnings = sum(1 for f in fs if isinstance(f, dict) and f.get("level") == "W")
    except (OSError, ValueError, AttributeError) as e:
        print(f"badge: cannot read {a.findings}: {e}", file=sys.stderr)
        return 1
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(badge(errors, warnings, a.label), fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"errors={errors}")
    print(f"warnings={warnings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
