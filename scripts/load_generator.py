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

    def bump(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, getattr(self, k) + v)


def register_user(session, base_url, stats):
    username = f"loadgen_{rand_suffix()}"
    try:
        r = session.post(
            f"{base_url}/api/register",
            json={"username": username, "password": "loadgen123"},
            timeout=10,
        )
        r.raise_for_status()
        stats.bump(registered=1)
        return r.json()["user_id"]
    except Exception:
        stats.bump(errors=1)
        return None


def virtual_user(base_url, stats, opts):
    session = requests.Session()
    # Stagger startup so exams overlap instead of all starting/finishing
    # in lockstep -- this is what keeps active_test_takers above 0.
    time.sleep(random.uniform(0, opts.stagger))

    user_id = register_user(session, base_url, stats)
    if user_id is None:
        return

    while not STOP.is_set():
        try:
            quizzes = session.get(f"{base_url}/api/quizzes", timeout=10).json()["quizzes"]
            if not quizzes:
                time.sleep(2)
                continue
            quiz_meta = random.choice(quizzes)
            quiz = session.get(f"{base_url}/api/quizzes/{quiz_meta['id']}", timeout=10).json()
            quiz_id = quiz["id"]

            session.post(
                f"{base_url}/api/exam/start",
                json={"quiz_id": quiz_id, "user_id": user_id},
                timeout=10,
            )
            stats.bump(started=1)

            think_time = random.uniform(opts.min_think, opts.max_think)
            tab_switch_count = 0

            if random.random() < opts.cheat_prob:
                # This attempt will tab-switch: split think_time into
                # segments and fire a switch between segments.
                num_switches = random.randint(1, 2)
                segment = think_time / (num_switches + 1)
                for _ in range(num_switches):
                    time.sleep(segment)
                    tab_switch_count += 1
                    stats.bump(tab_switches=1)
                    try:
                        session.post(
                            f"{base_url}/api/exam/tab_switch",
                            json={"user_id": user_id, "quiz_id": quiz_id},
                            timeout=10,
                        )
                    except Exception:
                        stats.bump(errors=1)
                time.sleep(segment)
            else:
                time.sleep(think_time)

            answers = {
                str(i): random.randint(0, len(q["options"]) - 1)
                for i, q in enumerate(quiz["questions"])
            }

            params = {"simulate_anomaly": "true"} if random.random() < opts.anomaly_prob else {}

            r = session.post(
                f"{base_url}/api/exam/submit",
                params=params,
                json={
                    "quiz_id": quiz_id,
                    "user_id": user_id,
                    "answers": answers,
                    "tab_switch_count": tab_switch_count,
                },
                timeout=15,
            )
            r.raise_for_status()
            result = r.json()
            stats.bump(submitted=1)
            if result.get("cheated"):
                stats.bump(cheated=1)

            time.sleep(random.uniform(opts.min_pause, opts.max_pause))

        except requests.RequestException:
            stats.bump(errors=1)
            time.sleep(2)
        except Exception:
            stats.bump(errors=1)
            time.sleep(2)


def reporter(stats, interval):
    while not STOP.wait(interval):
        with stats.lock:
            print(
                f"[stats] registered={stats.registered} started={stats.started} "
                f"submitted={stats.submitted} cheated={stats.cheated} "
                f"tab_switches={stats.tab_switches} errors={stats.errors}"
            )


def main():
    p = argparse.ArgumentParser(description="Continuous load generator for Veritas")
    p.add_argument("--url", default="http://localhost:8000", help="Veritas base URL")
    p.add_argument("--users", type=int, default=20, help="Concurrent virtual students")
    p.add_argument("--duration", type=float, default=0, help="Seconds to run; 0 = until Ctrl+C")
    p.add_argument("--stagger", type=float, default=15, help="Max random startup delay per user (s)")
    p.add_argument("--min-think", type=float, default=15, help="Min time 'in exam' per attempt (s)")
    p.add_argument("--max-think", type=float, default=60, help="Max time 'in exam' per attempt (s)")
    p.add_argument("--min-pause", type=float, default=3, help="Min pause between attempts (s)")
    p.add_argument("--max-pause", type=float, default=12, help="Max pause between attempts (s)")
    p.add_argument("--cheat-prob", type=float, default=0.15, help="Probability an attempt tab-switches")
    p.add_argument("--anomaly-prob", type=float, default=0.05, help="Probability of ?simulate_anomaly=true")
    p.add_argument("--report-every", type=float, default=10, help="Seconds between progress lines")
    args = p.parse_args()

    stats = Stats()
    print(f"Starting {args.users} virtual users against {args.url}")
    print("Press Ctrl+C to stop.\n")

    threads = [
        threading.Thread(target=virtual_user, args=(args.url, stats, args), daemon=True)
        for _ in range(args.users)
    ]
    for t in threads:
        t.start()
    threading.Thread(target=reporter, args=(stats, args.report_every), daemon=True).start()

    try:
        if args.duration > 0:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping... (letting in-flight requests finish)")
    finally:
        STOP.set()
        for t in threads:
            t.join(timeout=5)

    with stats.lock:
        print(
            f"\nFinal: registered={stats.registered} started={stats.started} "
            f"submitted={stats.submitted} cheated={stats.cheated} "
            f"tab_switches={stats.tab_switches} errors={stats.errors}"
        )


if __name__ == "__main__":
    main()