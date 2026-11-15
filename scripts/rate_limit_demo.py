"""Shows accepted requests, then 429 + Retry-After, then recovery. Limit: 5 logins / 60 s per client IP."""
import argparse
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000")
ap.add_argument("--calls", type=int, default=8)
a = ap.parse_args()
retry = None
for i in range(1, a.calls + 1):
    r = requests.post(f"{a.url}/api/v1/auth/login", json={"username": "nobody", "password": "wrong-pass"}, timeout=5)
    ra = r.headers.get("Retry-After")
    print(f"call {i}: HTTP {r.status_code}  Retry-After={ra}  body={r.json()['error']['code']}")
    if r.status_code == 429:
        retry = int(ra)
if retry:
    print(f"waiting {retry}s (Retry-After) ...")
    time.sleep(retry + 0.5)
    r = requests.post(f"{a.url}/api/v1/auth/login", json={"username": "nobody", "password": "wrong-pass"}, timeout=5)
    print(f"after waiting: HTTP {r.status_code} (allowed again -> 401 is the normal 'bad credentials' answer)")
