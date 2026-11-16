import grpc
import pytest

from tests.test_api import ANS


@pytest.fixture(scope="module")
def stub(client):
    from grpc_service.server import build_server
    from veritas.exam.v1 import exam_pb2_grpc
    server, port = build_server("127.0.0.1:0")
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield exam_pb2_grpc.ExamServiceStub(ch)
    server.stop(0)


def req(**kw):
    from veritas.exam.v1 import exam_pb2
    answers = kw.pop("answers", ANS)
    return exam_pb2.SubmitExamRequest(answers={int(k): v for k, v in answers.items()}, **kw)


def test_success_then_replay_shares_http_rules(client, stub, student, session_for):
    sid = session_for(student)
    r, call = stub.SubmitExam.with_call(req(session_id=sid, user_id=student["user_id"]), timeout=3,
                                        metadata=(("x-trace-id", "a" * 32),))
    assert r.status == "passed" and r.score == 100 and not r.replayed
    assert dict(call.trailing_metadata())["x-trace-id"] == "a" * 32
    again = stub.SubmitExam(req(session_id=sid, user_id=student["user_id"]), timeout=3)
    assert again.replayed and again.attempt_id == r.attempt_id
    # same source of truth: the HTTP side sees the gRPC-created attempt (and replays it)
    h = client.put(f"/api/v1/sessions/{sid}/attempt", json={"user_id": student["user_id"], "answers": ANS})
    assert h.status_code == 200 and h.json()["attempt_id"] == r.attempt_id


@pytest.mark.parametrize("kw,code", [
    (dict(session_id="nope"), grpc.StatusCode.NOT_FOUND),
    (dict(session_id=""), grpc.StatusCode.INVALID_ARGUMENT),
    (dict(answers={"0": 99}), grpc.StatusCode.INVALID_ARGUMENT),
    (dict(owner="other"), grpc.StatusCode.PERMISSION_DENIED),
])
def test_failures(client, stub, student, session_for, kw, code):
    sid = kw.pop("session_id", None)
    sid = session_for(student) if sid is None else sid
    uid = "someone-else" if kw.pop("owner", None) else student["user_id"]
    with pytest.raises(grpc.RpcError) as e:
        stub.SubmitExam(req(session_id=sid, user_id=uid, **kw), timeout=3)
    assert e.value.code() == code


def test_grpc_metrics_recorded(stub, student, session_for):
    from prometheus_client import generate_latest
    stub.SubmitExam(req(session_id=session_for(student), user_id=student["user_id"]), timeout=3)
    with pytest.raises(grpc.RpcError):
        stub.SubmitExam(req(session_id="nope", user_id="u"), timeout=3)
    text = generate_latest().decode()
    assert 'veritas_grpc_server_handled_total{grpc_code="OK",grpc_method="SubmitExam"}' in text
    assert 'grpc_code="NOT_FOUND"' in text
    assert "veritas_grpc_server_handling_seconds_bucket" in text


def test_get_quiz_read_only_call_reports_replica(stub):
    from veritas.exam.v1 import exam_pb2
    r, call = stub.GetQuiz.with_call(exam_pb2.GetQuizRequest(quiz_id="python_101"), timeout=3,
                                     metadata=(("x-trace-id", "b" * 32),))
    assert r.question_count == 5 and r.replica_id == "test"
    assert dict(call.initial_metadata())["x-replica-id"] == "test"
    with pytest.raises(grpc.RpcError) as e:
        stub.GetQuiz(exam_pb2.GetQuizRequest(quiz_id="nope"), timeout=3)
    assert e.value.code() == grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.RpcError) as e:
        stub.GetQuiz(exam_pb2.GetQuizRequest(), timeout=3)
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT
