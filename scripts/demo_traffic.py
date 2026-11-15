"""Repeatable success + failure traffic over BOTH transports, to populate Grafana/Kibana.

For every round it starts sessions over HTTP (one trace id per flow) and submits them via
HTTP or gRPC, plus deliberate failures. Prints expected-vs-actual and the trace id so the
same id can be searched in Kibana (trace.id) across veritas-app and veritas-grpc.

  python scripts/demo_traffic.py --rounds 5
"""
import _common  # noqa: F401
import argparse
import random
import uuid

import grpc
import requests

from grpc_client import parse_answers, submit
from veritas.exam.v1 import exam_pb2_grpc

GOOD = {"0": 1, "1": 1, "2": 0, "3": 2, "4": 1}
HTTP_TO_GRPC = {201: "OK", 200: "OK", 404: "NOT_FOUND", 403: "PERMISSION_DENIED",
                422: "INVALID_ARGUMENT", 409: "ALREADY_EXISTS"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="http://localhost:8000")
    ap.add_argument("--grpc", default="localhost:50051")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--anomaly", action="store_true", help="send simulate_anomaly on the first call of each round")
    a = ap.parse_args()
    s = requests.Session()

    def user():
        n = f"traffic_{uuid.uuid4().hex[:8]}"
        return s.post(f"{a.http}/api/v1/auth/register", json={"username": n, "password": "traffic123"},
                      timeout=5).json()["user_id"]

    def session(uid, trace):
        r = s.post(f"{a.http}/api/v1/quizzes/python_101/sessions", json={"user_id": uid},
                   headers={"X-Trace-Id": trace}, timeout=5)
        return r.json()["session_id"]

    def http_submit(sid, uid, answers, trace, anomaly=False):
        r = s.put(f"{a.http}/api/v1/sessions/{sid}/attempt", params={"simulate_anomaly": str(anomaly).lower()},
                  json={"user_id": uid, "answers": answers}, headers={"X-Trace-Id": trace}, timeout=10)
        return r.status_code

    alice, mallory = user(), user()
    rows = []
    with grpc.insecure_channel(a.grpc) as ch:
        stub = exam_pb2_grpc.ExamServiceStub(ch)

        def g(sid, uid, answers, trace, anomaly=False):
            _, code, _, _, _ = submit(stub, sid, uid, {int(k): v for k, v in answers.items()}, 5, trace,
                                      anomaly=anomaly)
            return code

        for rnd in range(a.rounds):
            wrong = {k: (v + 1) % 4 for k, v in GOOD.items()}
            answers = random.choice([GOOD, wrong])
            for name, call in (("HTTP", http_submit), ("gRPC", g)):
                tr = uuid.uuid4().hex
                sid = session(alice, tr)
                anomaly = a.anomaly and rnd == 0
                got = call(sid, alice, answers, tr, anomaly)
                rows.append((name, "success", "201/OK", got, tr))
                got = call(sid, alice, answers, tr)               # replay
                rows.append((name, "replay", "200/OK", got, tr))
                got = call("does-not-exist", alice, answers, tr)
                rows.append((name, "unknown session", "404/NOT_FOUND", got, tr))
                got = call(session(alice, tr), mallory, answers, tr)
                rows.append((name, "wrong owner", "403/PERMISSION_DENIED", got, tr))
                got = call(session(alice, tr), alice, {"0": 99}, tr)
                rows.append((name, "bad option index", "422/INVALID_ARGUMENT", got, tr))

    print(f"{'transport':9} {'case':17} {'expected':22} {'actual':18} trace_id")
    for t, case, exp, got, tr in rows:
        print(f"{t:9} {case:17} {exp:22} {got!s:18} {tr}")


if __name__ == "__main__":
    main()
