"""A stand-in for the gitleaks binary, for tests that must not depend on the real one.

Usage by the tests: a wrapper script calls main(sys.argv[1:], wrapper_path) with the arguments `dir SRC --no-banner ... -r REPORT
--exit-code 99 ...` (the checker runs the binary with an empty environment, so behaviour switches are files next to the wrapper).
Every line of every file under SRC that contains the marker is a finding (File, StartLine, EndLine); exit code 99 when there is one,
0 otherwise. A file `<wrapper>.fail` makes it exit 7 (a run that fails), `<wrapper>.sleep` (holding the seconds) makes it sleep.
"""

import json
import os
import sys
import time

MARKER = "SECRET_TOKEN"


def main(argv, wrapper=""):
    if wrapper and os.path.exists(wrapper + ".sleep"):
        with open(wrapper + ".sleep", encoding="utf-8") as fh:
            time.sleep(float(fh.read()))
    if wrapper and os.path.exists(wrapper + ".fail"):
        sys.stderr.write("stand-in failure\n")
        return 7
    src = argv[argv.index("dir") + 1]
    rep = argv[argv.index("-r") + 1]
    code = int(argv[argv.index("--exit-code") + 1])
    found = []
    for dp, _dn, fn in os.walk(src):
        for f in sorted(fn):
            with open(os.path.join(dp, f), encoding="utf-8") as fh:
                for n, line in enumerate(fh.read().split("\n"), 1):
                    if MARKER in line:
                        found.append({"File": os.path.join(dp, f), "StartLine": n, "EndLine": n})
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump(found, fh)
    return code if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
