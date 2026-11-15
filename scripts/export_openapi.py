"""Regenerate openapi/openapi.json from the FastAPI app (the spec is derived from the
same route/model definitions that serve traffic, so it cannot drift silently --
tests/test_api.py::test_openapi_matches_committed_spec enforces that)."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.main import app  # noqa: E402

out = os.path.join(ROOT, "openapi", "openapi.json")
with open(out, "w") as f:
    json.dump(app.openapi(), f, indent=2, sort_keys=False)
    f.write("\n")
print("wrote", out)
