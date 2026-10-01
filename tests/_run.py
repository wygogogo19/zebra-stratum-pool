#!/usr/bin/env python3
"""Run the whole suite with the stdlib runner (used when pytest is unavailable, e.g. offline hosts).

    python3 tests/_run.py                     # plain run
    python3 -m trace --count --summary --coverdir=/tmp/cov tests/_run.py   # line coverage
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if __name__ == "__main__":
    suite = unittest.TestLoader().discover(HERE, top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
