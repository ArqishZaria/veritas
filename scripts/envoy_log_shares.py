"""Count Envoy access-log events per interface and replica (cross-check against the dashboard / client numbers).
  docker logs veritas-envoy --since 3m 2>&1 | python scripts/envoy_log_shares.py [--path /api/v1/quizzes/]"""
import argparse
import collections
import json
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--path", default="", help="only count events whose url.path starts with this")
a = ap.parse_args()
c, first, last = collections.Counter(), None, None
for line in sys.stdin:
    try:
        j = json.loads(line)
    except ValueError:
        continue
    if j.get("service.name") != "veritas-envoy" or not j.get("url.path", "").startswith(a.path):
        continue
    c[(str(j.get("interface") or "-"), str(j.get("replica.id") or "-"))] += 1
    first, last = first or j.get("@timestamp"), j.get("@timestamp")
print(f"time range in log: {first} .. {last}")
for iface in sorted({k[0] for k in c}):
    tot = sum(v for k, v in c.items() if k[0] == iface)
    for (i, r), v in sorted(c.items()):
        if i == iface:
            print(f"{iface:5} replica {r:4} {v:7d}  {100 * v / tot:5.1f}%")
    print(f"{iface:5} total        {tot:7d}")
