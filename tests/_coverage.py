#!/usr/bin/env python3
"""Minimal statement-coverage harness for machines without pip (no pytest-cov available).

`python3 -m trace` reports "100%" for code with unexecuted branches because it treats `else:`,
`try:`/`except ...:` and unreached branch bodies as non-executable lines. This harness instead derives the
statement set from each code object's line table (the same source `coverage.py` uses) and records executed
lines with `sys.settrace`. CI uses `pytest-cov`; this exists so the suite can also be measured offline.

    python3 tests/_coverage.py            # run the suite and print per-module statement coverage
"""
from __future__ import annotations

import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULES = ["pool.py", "build_coinbase.py", "zcash_v6.py", "equihash_verify.py"]


def statement_lines(path: str) -> set[int]:
    """Line numbers that carry bytecode (statements), walked recursively over nested code objects."""
    with open(path, encoding="utf-8") as fh:
        code = compile(fh.read(), path, "exec")
    lines: set[int] = set()

    def walk(co: types.CodeType) -> None:
        for _start, _end, lineno in co.co_lines():
            if lineno:
                lines.add(lineno)
        for const in co.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(code)
    return lines


def run() -> int:
    targets = {os.path.abspath(os.path.join(ROOT, m)): m for m in MODULES}
    executed: dict[str, set[int]] = {m: set() for m in MODULES}

    def tracer(frame, event, arg):
        if event == "line":
            path = frame.f_code.co_filename
            if path in targets:
                executed[targets[path]].add(frame.f_lineno)
        return tracer

    # Drop the modules so their import-time lines are traced as well (discovery alone would reuse them).
    for name in MODULES:
        sys.modules.pop(name[:-3], None)
    sys.settrace(tracer)
    try:
        suite = unittest.TestLoader().discover(HERE, top_level_dir=ROOT)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        sys.settrace(None)

    print("\nStatement coverage (same metric coverage.py reports)")
    print("%-22s %8s %8s %9s" % ("module", "stmts", "hit", "cover"))
    total_stmt = total_hit = 0
    for name in MODULES:
        stmts = statement_lines(os.path.join(ROOT, name))
        hits = stmts & executed[name]
        total_stmt += len(stmts)
        total_hit += len(hits)
        print("%-22s %8d %8d %8.1f%%" % (name, len(stmts), len(hits),
                                         100.0 * len(hits) / max(1, len(stmts))))
        missing = sorted(stmts - hits)
        if missing:
            print("     missed lines: %s%s" % (missing[:15], " ..." if len(missing) > 15 else ""))
    print("%-22s %8d %8d %8.1f%%" % ("TOTAL", total_stmt, total_hit,
                                     100.0 * total_hit / max(1, total_stmt)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
