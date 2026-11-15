"""Python gRPC client for ExamService.SubmitExam (direct call, no browser).

  python scripts/grpc_client.py --session-id <id> --user-id <id> --answers 0=1,1=1,2=0,3=2,4=1 \
         --deadline 3 [--trace-id <32 hex>] [--target localhost:50051]
"""
import _common  # noqa: F401  (adds repo root and gen/ to sys.path)
import argparse
import sys
import time
import uuid

import grpc

from veritas.exam.v1 import exam_pb2, exam_pb2_grpc


def parse_answers(text):
    out = {}
    for pair in filter(None, (text or "").split(",")):
        k, v = pair.split("=")
        out[int(k)] = int(v)
    return out


def submit(stub, session_id, user_id, answers, deadline, trace_id, tabs=0, anomaly=False):
    """Returns (response|None, code_name, details, trace_id_from_server, client_ms)."""
    req = exam_pb2.SubmitExamRequest(session_id=session_id, user_id=user_id, answers=answers,
                                     tab_switch_count=tabs, simulate_anomaly=anomaly)
    t0 = time.perf_counter()
    try:
        resp, call = stub.SubmitExam.with_call(req, timeout=deadline, metadata=(("x-trace-id", trace_id),))
        ms = (time.perf_counter() - t0) * 1000
        return resp, "OK", "", dict(call.trailing_metadata() or ()).get("x-trace-id"), ms
    except grpc.RpcError as e:
        ms = (time.perf_counter() - t0) * 1000
        return None, e.code().name, e.details(), dict(e.trailing_metadata() or ()).get("x-trace-id"), ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="localhost:50051")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--answers", default="")
    ap.add_argument("--tab-switches", type=int, default=0)
    ap.add_argument("--deadline", type=float, default=3.0, help="seconds; DEADLINE_EXCEEDED if exceeded")
    ap.add_argument("--trace-id", default=uuid.uuid4().hex)
    ap.add_argument("--simulate-anomaly", action="store_true")
    a = ap.parse_args()
    with grpc.insecure_channel(a.target) as ch:
        stub = exam_pb2_grpc.ExamServiceStub(ch)
        resp, code, details, srv_trace, ms = submit(stub, a.session_id, a.user_id, parse_answers(a.answers),
                                                    a.deadline, a.trace_id, a.tab_switches, a.simulate_anomaly)
    print(f"status={code} client_latency={ms:.0f}ms trace_id={srv_trace or a.trace_id}")
    if resp is not None:
        print(f"score={resp.score} status={resp.status} correct={resp.correct}/{resp.total} "
              f"cheated={resp.cheated} replayed={resp.replayed} attempt_id={resp.attempt_id}")
        return 0
    print(f"error: {details}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
