"""A small service-style caller for the Veritas HTTP API with:
  * timeout, * limited retry + exponential backoff (full jitter), * circuit breaker.

Operation under test: PUT /api/v1/sessions/{id}/attempt  (idempotent, so retrying is safe).
POST create-session is NOT retried (not idempotent -> a retry could create a second session).

Run:  python scripts/http_caller.py --url http://localhost:8000
"""
import argparse
import random
import time
import uuid

import requests

# ----------------------------- settings (also printed at start) -------------
CONNECT_TIMEOUT_S = 1.0
READ_TIMEOUT_S = 2.0
MAX_RETRIES = 2              # => at most 3 attempts per call
BACKOFF_BASE_S = 0.2         # sleep = uniform(0, min(CAP, BASE * 2**retry))
BACKOFF_CAP_S = 2.0
BREAKER_FAILURE_THRESHOLD = 3   # consecutive failed attempts -> OPEN
BREAKER_RECOVERY_S = 5.0        # OPEN -> HALF_OPEN after this long; one probe decides
RETRYABLE_STATUS = {502, 503, 504}
ANSWERS = {"0": 1, "1": 1, "2": 0, "3": 2, "4": 1}


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, threshold, recovery_s, clock=time.monotonic):
        self.threshold, self.recovery_s, self.clock = threshold, recovery_s, clock
        self.state, self.failures, self.opened_at = "CLOSED", 0, 0.0

    def allow(self):
        if self.state == "OPEN":
            if self.clock() - self.opened_at >= self.recovery_s:
                self.state = "HALF_OPEN"
                print(f"    [breaker] recovery period over -> HALF_OPEN (one probe call allowed)")
                return True
            return False
        return True

    def success(self):
        if self.state != "CLOSED":
            print("    [breaker] probe succeeded -> CLOSED")
        self.state, self.failures = "CLOSED", 0

    def failure(self):
        self.failures += 1
        if self.state == "HALF_OPEN" or self.failures >= self.threshold:
            if self.state != "OPEN":
                print(f"    [breaker] {self.failures} consecutive failures -> OPEN for {self.recovery_s}s")
            self.state, self.opened_at = "OPEN", self.clock()


class ResilientClient:
    def __init__(self, base_url):
        self.base, self.http = base_url.rstrip("/"), requests.Session()
        self.breaker = CircuitBreaker(BREAKER_FAILURE_THRESHOLD, BREAKER_RECOVERY_S)

    def call(self, method, path, *, idempotent, trace_id=None, **kw):
        retries = MAX_RETRIES if idempotent else 0
        headers = {"X-Trace-Id": trace_id or uuid.uuid4().hex}
        for attempt in range(retries + 1):
            if not self.breaker.allow():
                print(f"  [{method} {path.split('/')[-1]}] BLOCKED: circuit is OPEN (no request sent)")
                raise CircuitOpenError(path)
            t0 = time.perf_counter()
            try:
                r = self.http.request(method, self.base + path, headers=headers,
                                      timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), **kw)
            except (requests.Timeout, requests.ConnectionError) as e:
                outcome, retryable, resp = f"{type(e).__name__}", True, None
            else:
                ms = (time.perf_counter() - t0) * 1000
                resp = r
                outcome = f"HTTP {r.status_code} in {ms:.0f}ms"
                retryable = r.status_code in RETRYABLE_STATUS
                if r.status_code >= 500:
                    self.breaker.failure()
                else:
                    self.breaker.success()   # 2xx and 4xx both mean "server is healthy"
                if not retryable:
                    print(f"  [attempt {attempt + 1}/{retries + 1}] {method} {path.split('/')[-1]} -> {outcome}")
                    return r
            if resp is None:
                self.breaker.failure()
            print(f"  [attempt {attempt + 1}/{retries + 1}] {method} {path.split('/')[-1]} -> {outcome} "
                  f"(breaker={self.breaker.state}, failures={self.breaker.failures})")
            if not retryable or attempt == retries:
                if resp is not None and attempt == retries:
                    return resp
                raise requests.RequestException(f"{outcome}; gave up after {attempt + 1} attempt(s)")
            delay = random.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** attempt))
            print(f"      retrying in {delay * 1000:.0f}ms (backoff)")
            time.sleep(delay)


def set_fault(client, admin_id, mode, remaining=0, delay=0.0):
    r = client.http.put(f"{client.base}/api/v1/admin/fault-injection", timeout=5,
                        json={"admin_id": admin_id, "mode": mode, "remaining": remaining, "delay_seconds": delay})
    r.raise_for_status()


def submit_new(client, user_id, label):
    s = client.http.post(f"{client.base}/api/v1/quizzes/python_101/sessions", json={"user_id": user_id}, timeout=5)
    s.raise_for_status()
    sid = s.json()["session_id"]
    print(f"{label}: submit session {sid[:8]}…")
    try:
        r = client.call("PUT", f"/api/v1/sessions/{sid}/attempt", idempotent=True,
                        json={"user_id": user_id, "answers": ANSWERS})
        print(f"  => final result: HTTP {r.status_code} {r.json() if r.ok else r.text[:80]}\n")
        return sid, r
    except (CircuitOpenError, requests.RequestException) as e:
        print(f"  => FAILED: {e!r}\n")
        return sid, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--admin-pass", default="admin123")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)
    print(f"settings: timeout(connect={CONNECT_TIMEOUT_S}s, read={READ_TIMEOUT_S}s) retries={MAX_RETRIES} "
          f"backoff=full-jitter base={BACKOFF_BASE_S}s cap={BACKOFF_CAP_S}s "
          f"breaker(threshold={BREAKER_FAILURE_THRESHOLD}, recovery={BREAKER_RECOVERY_S}s)\n")
    c = ResilientClient(a.url)
    admin = c.http.post(f"{c.base}/api/v1/auth/login", timeout=5,
                        json={"username": a.admin_user, "password": a.admin_pass}).json()["user_id"]
    me = c.http.post(f"{c.base}/api/v1/auth/register", timeout=5,
                     json={"username": f"caller_{uuid.uuid4().hex[:8]}", "password": "caller123"}).json()["user_id"]
    ok_sessions = set()

    print("=== 1. Healthy calls ===")
    for i in range(2):
        sid, r = submit_new(c, me, f"call {i + 1}")
        r is not None and ok_sessions.add(sid)

    print("=== 2. Transient failure: server returns 503 twice, third attempt succeeds ===")
    set_fault(c, admin, "error", remaining=2)
    sid, r = submit_new(c, me, "call 3")
    r is not None and ok_sessions.add(sid)

    print("=== 3. Timeout + idempotent retry: first request stalls 3s (> read timeout 2s) ===")
    set_fault(c, admin, "slow", remaining=1, delay=3.0)
    sid, r = submit_new(c, me, "call 4")
    r is not None and ok_sessions.add(sid)
    time.sleep(2)  # let the stalled first request finish on the server (it will be a replay)
    n = len(c.http.get(f"{c.base}/api/v1/users/{me}/attempts", timeout=5).json()["attempts"])
    print(f"server recorded {n} attempts for {len(ok_sessions)} submitted sessions -> no duplicates: {n == len(ok_sessions)}\n")

    print("=== 4. Sustained outage: breaker opens, calls blocked, then recovery ===")
    set_fault(c, admin, "error", remaining=1000)
    submit_new(c, me, "call 5 (outage)")
    submit_new(c, me, "call 6 (breaker open)")
    submit_new(c, me, "call 7 (breaker open)")
    print(f"... server heals; waiting {BREAKER_RECOVERY_S}s recovery period ...")
    set_fault(c, admin, "off")
    time.sleep(BREAKER_RECOVERY_S + 0.2)
    sid, r = submit_new(c, me, "call 8 (half-open probe)")
    sid, r = submit_new(c, me, "call 9 (back to normal)")


if __name__ == "__main__":
    main()
