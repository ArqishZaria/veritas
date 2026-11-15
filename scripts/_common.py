import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "gen")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
