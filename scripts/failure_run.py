"""D3 helper: runs the load client and, at fixed offsets, stops and restarts replica B; prints UTC timestamps so
removal/recovery times can be read off the dashboard.
  python scripts/failure_run.py http --rate 100 --duration 120 --stop-at 30 --start-at 75 --out results/d3-http.json"""
import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("interface", choices=["http", "grpc"])
ap.add_argument("--rate", default="100")
ap.add_argument("--duration", default="120")
ap.add_argument("--stop-at", type=float, default=30)
ap.add_argument("--start-at", type=float, default=75)
ap.add_argument("--out", default=None)
a = ap.parse_args()


def stamp():
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def at(offset, action):
    time.sleep(offset)
    print(f"[{stamp()}] docker compose {action} veritas-b", flush=True)
    subprocess.check_call(["docker", "compose", action, "veritas-b"], cwd=ROOT)


t0 = time.time()
cmd = [sys.executable, os.path.join(ROOT, "scripts", "load_client.py"), a.interface, "--rate", a.rate, "--duration", a.duration,
       "--label", f"D3 failure: stop B at +{a.stop_at}s, start at +{a.start_at}s"]
if a.out:
    cmd += ["--out", a.out]
print(f"[{stamp()}] load starts")
proc = subprocess.Popen(cmd)
threading.Thread(target=at, args=(a.stop_at, "stop"), daemon=True).start()
threading.Thread(target=lambda: at(a.start_at, "start"), daemon=True).start()
proc.wait()
print(f"[{stamp()}] load finished")
