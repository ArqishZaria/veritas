"""Load an Envoy scenario:  python scripts/scenario.py a-only|rr|weighted|least-request|health
Writes ENVOY_SCENARIO to .env (so later plain `docker compose up` keeps it) and recreates only the envoy container."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = ("a-only", "rr", "weighted", "least-request", "health")
if len(sys.argv) != 2 or sys.argv[1] not in NAMES:
    sys.exit(f"usage: python scripts/scenario.py {'|'.join(NAMES)}")
open(os.path.join(ROOT, ".env"), "w").write(f"ENVOY_SCENARIO={sys.argv[1]}\n")
subprocess.check_call(["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", "envoy"], cwd=ROOT)
print(f"envoy now runs scenario '{sys.argv[1]}' (envoy/{sys.argv[1]}.yaml)")
