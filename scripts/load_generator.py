import argparse
import random
import string
import threading
import time
from dataclasses import dataclass, field

import requests

STOP = threading.Event()


def rand_suffix(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


@dataclass
class Stats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    registered: int = 0
    started: int = 0
    submitted: int = 0
    cheated: int = 0
    tab_switches: int = 0
    errors: int = 0

    def bump(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


def virtual_user(base_url, stats, opts):
    s = requests.Session()
    time.sleep(random.uniform(0, opts.stagger))
    try:
        r = s.post(f"{base_url}/api/v1/auth/register",
                   json={"username": f"loadgen_{rand_suffix()}", "password": "loadgen123"}, timeout=10)
        r.raise_for_status()
        user_id = r.json()["user_id"]
        stats.bump(registered=1)
    except Exception:
        stats.bump(errors=1)
        return
    while not STOP.is_set():
        try:
            quizzes = s.get(f"{base_url}/api/v1/quizzes", timeout=10).json()["quizzes"]
            if not quizzes:
                time.sleep(2)
                continue
            quiz = s.get(f"{base_url}/api/v1/quizzes/{random.choice(quizzes)['id']}", timeout=10).json()
            r = s.post(f"{base_url}/api/v1/quizzes/{quiz['id']}/sessions", json={"user_id": user_id}, timeout=10)
            r.raise_for_status()
            sid = r.json()["session_id"]
            stats.bump(started=1)
            think = random.uniform(opts.min_think, opts.max_think)
            if random.random() < opts.cheat_prob:
                n = random.randint(1, 2)
                for _ in range(n):
                    time.sleep(think / (n + 1))
                    s.post(f"{base_url}/api/v1/sessions/{sid}/tab-switches", json={"user_id": user_id}, timeout=10)
                    stats.bump(tab_switches=1)
                time.sleep(think / (n + 1))
            else:
                time.sleep(think)
            answers = {str(i): random.randint(0, len(q["options"]) - 1) for i, q in enumerate(quiz["questions"])}
            params = {"simulate_anomaly": "true"} if random.random() < opts.anomaly_prob else {}
            r = s.put(f"{base_url}/api/v1/sessions/{sid}/attempt", params=params,
                      json={"user_id": user_id, "answers": answers}, timeout=15)
            r.raise_for_status()
            stats.bump(submitted=1)
            if r.json().get("cheated"):
                stats.bump(cheated=1)
            time.sleep(random.uniform(opts.min_pause, opts.max_pause))
        except Exception:
            stats.bump(errors=1)
            time.sleep(2)


def reporter(stats, interval):
    while not STOP.wait(interval):
        with stats.lock:
            print(f"[stats] registered={stats.registered} started={stats.started} submitted={stats.submitted} "
                  f"cheated={stats.cheated} tab_switches={stats.tab_switches} errors={stats.errors}")


def main():
    p = argparse.ArgumentParser(description="Continuous load generator for Veritas (API v1)")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--users", type=int, default=20)
    p.add_argument("--duration", type=float, default=0)
    p.add_argument("--stagger", type=float, default=15)
    p.add_argument("--min-think", type=float, default=15)
    p.add_argument("--max-think", type=float, default=60)
    p.add_argument("--min-pause", type=float, default=3)
    p.add_argument("--max-pause", type=float, default=12)
    p.add_argument("--cheat-prob", type=float, default=0.15)
    p.add_argument("--anomaly-prob", type=float, default=0.05)
    p.add_argument("--report-every", type=float, default=10)
    args = p.parse_args()
    stats = Stats()
    threads = [threading.Thread(target=virtual_user, args=(args.url, stats, args), daemon=True)
               for _ in range(args.users)]
    for t in threads:
        t.start()
    threading.Thread(target=reporter, args=(stats, args.report_every), daemon=True).start()
    try:
        time.sleep(args.duration) if args.duration > 0 else [time.sleep(1) for _ in iter(int, 1)]
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        STOP.set()
        for t in threads:
            t.join(timeout=5)
    print(f"Final: registered={stats.registered} started={stats.started} submitted={stats.submitted} "
          f"cheated={stats.cheated} tab_switches={stats.tab_switches} errors={stats.errors}")


if __name__ == "__main__":
    main()
