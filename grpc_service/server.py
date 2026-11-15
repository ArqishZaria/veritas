"""gRPC ExamService. Runs in its own container, shares the DB and the business
rules (app.core.exam) with the HTTP app. Prometheus metrics on :9101."""
import os
import signal
import time
from concurrent import futures

import grpc
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from app.core import exam
from app.core.db import wait_for_db
from app.core.logging_ecs import ecs_log, is_valid_trace_id, new_trace_id
from veritas.exam.v1 import exam_pb2, exam_pb2_grpc

GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))
METRICS_PORT = int(os.environ.get("GRPC_METRICS_PORT", "9101"))
MAX_WORKERS = int(os.environ.get("GRPC_MAX_WORKERS", "10"))

# ---- metrics: labels are bounded (method name x status-code name) ----------
GRPC_HANDLED_TOTAL = Counter("veritas_grpc_server_handled_total",
                             "RPCs completed by the server, by method and gRPC status code.",
                             ["grpc_method", "grpc_code"])
GRPC_HANDLING_SECONDS = Histogram("veritas_grpc_server_handling_seconds",
                                  "Server-side handler duration (excludes network and (de)serialization).",
                                  ["grpc_method"],
                                  buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0))
GRPC_IN_FLIGHT = Gauge("veritas_grpc_server_in_flight", "RPCs currently executing on the server.",
                       ["grpc_method"])
for _code in ("OK", "NOT_FOUND", "PERMISSION_DENIED", "INVALID_ARGUMENT", "ALREADY_EXISTS", "INTERNAL"):
    GRPC_HANDLED_TOTAL.labels("SubmitExam", _code)
GRPC_HANDLING_SECONDS.labels("SubmitExam")
GRPC_IN_FLIGHT.labels("SubmitExam").set(0)


class MetricsInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, details):
        handler = continuation(details)
        if handler is None or handler.unary_unary is None:
            return handler
        method = details.method.rsplit("/", 1)[-1]
        inner = handler.unary_unary

        def wrapped(request, context):
            GRPC_IN_FLIGHT.labels(method).inc()
            start = time.perf_counter()
            code = grpc.StatusCode.OK
            try:
                response = inner(request, context)
                code = context.code() or grpc.StatusCode.OK
                return response
            except BaseException:
                code = context.code() or grpc.StatusCode.UNKNOWN
                raise
            finally:
                GRPC_IN_FLIGHT.labels(method).dec()
                GRPC_HANDLING_SECONDS.labels(method).observe(time.perf_counter() - start)
                GRPC_HANDLED_TOTAL.labels(method, code.name).inc()

        return grpc.unary_unary_rpc_method_handler(
            wrapped, request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer)


DOMAIN_TO_GRPC = {
    "NOT_FOUND": grpc.StatusCode.NOT_FOUND,
    "PERMISSION_DENIED": grpc.StatusCode.PERMISSION_DENIED,
    "INVALID_ARGUMENT": grpc.StatusCode.INVALID_ARGUMENT,
    "CONFLICT": grpc.StatusCode.ALREADY_EXISTS,
}
_RPC = {"rpc.system": "grpc", "rpc.service": "veritas.exam.v1.ExamService", "rpc.method": "SubmitExam"}


class ExamService(exam_pb2_grpc.ExamServiceServicer):
    def SubmitExam(self, request, context):
        incoming = dict(context.invocation_metadata()).get("x-trace-id", "")
        trace_id = incoming if is_valid_trace_id(incoming) else new_trace_id()
        context.set_trailing_metadata((("x-trace-id", trace_id),))

        if not request.session_id or not request.user_id:
            ecs_log("warn", "SubmitExam rejected: session_id and user_id are required", trace_id=trace_id,
                    event_action="rpc_rejected", **_RPC, **{"grpc.status_code": "INVALID_ARGUMENT"})
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id and user_id are required")
        try:
            result, replayed = exam.submit_attempt(
                request.session_id, request.user_id, dict(request.answers),
                request.tab_switch_count, request.simulate_anomaly, trace_id)
        except exam.DomainError as e:
            code = DOMAIN_TO_GRPC[e.code]
            ecs_log("warn", f"SubmitExam rejected: {e.message}", trace_id=trace_id, student_id=request.user_id,
                    event_action="rpc_rejected", **_RPC, **{"grpc.status_code": code.name})
            context.abort(code, e.message)
        except Exception as e:  # noqa: BLE001
            ecs_log("error", f"SubmitExam failed: {e!r}", trace_id=trace_id, event_action="rpc_failed",
                    **_RPC, **{"grpc.status_code": "INTERNAL"})
            context.abort(grpc.StatusCode.INTERNAL, "internal error")
        return exam_pb2.SubmitExamResponse(**result, replayed=replayed)


def build_server(address: str = None):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=MAX_WORKERS), interceptors=[MetricsInterceptor()])
    exam_pb2_grpc.add_ExamServiceServicer_to_server(ExamService(), server)
    port = server.add_insecure_port(address or f"[::]:{GRPC_PORT}")
    return server, port


def serve():
    wait_for_db()
    server, port = build_server()
    start_http_server(METRICS_PORT)
    server.start()
    ecs_log("info", f"gRPC ExamService listening on :{port}, metrics on :{METRICS_PORT}",
            event_action="grpc_started")
    signal.signal(signal.SIGTERM, lambda *_: server.stop(5))
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
