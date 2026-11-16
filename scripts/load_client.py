"""Fixed-rate, OPEN-LOOP load client (A3): requests are sent on a schedule (i / rate seconds after start) and
never wait for earlier responses, so a slow replica cannot slow the sending down. No retries, no circuit breaker.

  HTTP: GET  /api/v1/quizzes/{quiz_id}          python scripts/load_client.py http --rate 100 --duration 120
  gRPC: ExamService/GetQuiz (ONE connection)    python scripts/load_client.py grpc --rate 100 --duration 120
Every request carries a fresh 32-hex trace id; the serving replica is read from X-Replica-Id / x-replica-id."""
import _common  # noqa: F401
import argparse
import asyncio
import collections
import json
import math
import os
import time
import uuid

import grpc
import httpx

from veritas.exam.v1 import exam_pb2, exam_pb2_grpc


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


class Recorder:
    def __init__(self):
        self.ok_lat, self.all_lat = [], []
        self.outcomes, self.replicas = collections.Counter(), collections.Counter()
        self.sample_trace = {}
        self.failed = 0
        self.max_lag = 0.0

    def add(self, trace_id, latency, outcome, replica, ok):
        self.all_lat.append(latency)
        self.outcomes[outcome] += 1
        if ok:
            self.ok_lat.append(latency)
            self.replicas[replica] += 1
            self.sample_trace.setdefault(replica, trace_id)
        else:
            self.failed += 1
            self.sample_trace.setdefault("failed", trace_id)


async def fire(a, one):
    """Open loop: schedule request i at start + i/rate; late iterations are sent immediately (lag is recorded)."""
    total = int(a.rate * a.duration)
    start, tasks, lag = time.perf_counter(), [], 0.0
    for i in range(total):
        wait = start + i / a.rate - time.perf_counter()
        if wait > 0:
            await asyncio.sleep(wait)
        else:
            lag = max(lag, -wait)
        tasks.append(asyncio.create_task(one()))
    send_elapsed = time.perf_counter() - start
    await asyncio.gather(*tasks)
    return total, send_elapsed, lag


def md_get(md, key):
    for k, v in (md or ()):
        if k == key:
            return v
    return "?"


async def run_http(a, rec):
    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=a.url, timeout=a.timeout, limits=limits) as c:
        async def one():
            tid, t0 = uuid.uuid4().hex, time.perf_counter()
            try:
                r = await c.get(f"/api/v1/quizzes/{a.quiz_id}", headers={"X-Trace-Id": tid})
                rec.add(tid, time.perf_counter() - t0, f"HTTP {r.status_code}",
                        r.headers.get("x-replica-id", "?"), r.status_code == 200)
            except Exception as e:  # timeouts / connection errors count as failures
                rec.add(tid, time.perf_counter() - t0, type(e).__name__, "none", False)
        return await fire(a, one)


async def run_grpc(a, rec):
    async with grpc.aio.insecure_channel(a.target) as ch:   # ONE connection for every call
        stub = exam_pb2_grpc.ExamServiceStub(ch)

        async def one():
            tid, t0 = uuid.uuid4().hex, time.perf_counter()
            call = stub.GetQuiz(exam_pb2.GetQuizRequest(quiz_id=a.quiz_id), timeout=a.timeout,
                                metadata=(("x-trace-id", tid),))
            try:
                await call
                rec.add(tid, time.perf_counter() - t0, "OK", md_get(await call.initial_metadata(), "x-replica-id"), True)
            except grpc.aio.AioRpcError as e:
                rec.add(tid, time.perf_counter() - t0, e.code().name, md_get(e.initial_metadata(), "x-replica-id"), False)
            except Exception as e:
                rec.add(tid, time.perf_counter() - t0, type(e).__name__, "none", False)
        return await fire(a, one)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("interface", choices=["http", "grpc"])
    ap.add_argument("--url", default="http://localhost:8000", help="HTTP base URL (Envoy listener)")
    ap.add_argument("--target", default="localhost:50051", help="gRPC target (Envoy listener)")
    ap.add_argument("--rate", type=float, default=100, help="target requests per second")
    ap.add_argument("--duration", type=float, default=60, help="seconds of sending")
    ap.add_argument("--timeout", type=float, default=5, help="per-request timeout, seconds")
    ap.add_argument("--quiz-id", default="python_101")
    ap.add_argument("--label", default="", help="free text stored in the JSON result (e.g. 'D2 least-request, B+200ms')")
    ap.add_argument("--out", help="write the result as JSON (e.g. results/c-rr-http.json)")
    a = ap.parse_args()
    rec = Recorder()
    t_start = time.time()
    sent, send_elapsed, lag = asyncio.run((run_http if a.interface == "http" else run_grpc)(a, rec))
    ok = len(rec.ok_lat)
    res = {"label": a.label, "interface": a.interface, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
           "target_rps": a.rate, "duration_s": a.duration, "sent": sent, "achieved_send_rps": round(sent / send_elapsed, 2),
           "max_send_lag_ms": round(lag * 1000, 1), "success": ok, "failed": rec.failed,
           "error_rate_pct": round(100 * rec.failed / sent, 2) if sent else 0.0,
           "client_p50_ms": round(pct(rec.ok_lat, 50) * 1000, 1) if ok else None,
           "client_p95_ms": round(pct(rec.ok_lat, 95) * 1000, 1) if ok else None,
           "client_p99_ms": round(pct(rec.ok_lat, 99) * 1000, 1) if ok else None,
           "client_p95_all_ms": round(pct(rec.all_lat, 95) * 1000, 1) if rec.all_lat else None,
           "outcomes": dict(rec.outcomes),
           "replica_counts": dict(rec.replicas),
           "replica_share_pct": {k: round(100 * v / ok, 1) for k, v in sorted(rec.replicas.items())} if ok else {},
           "sample_trace_ids": rec.sample_trace}
    print(json.dumps(res, indent=2))
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
