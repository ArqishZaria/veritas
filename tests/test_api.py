import json
import os

ANS = {"0": 1, "1": 1, "2": 0, "3": 2, "4": 1}  # python_101, all correct


def put_attempt(client, sid, uid, answers=ANS, **kw):
    return client.put(f"/api/v1/sessions/{sid}/attempt", json={"user_id": uid, "answers": answers, **kw})


def test_valid_flow(client, student, session_for):
    sid = session_for(student)
    r = put_attempt(client, sid, student["user_id"])
    assert r.status_code == 201
    body = r.json()
    assert body["score"] == 100.0 and body["status"] == "passed" and body["total"] == 5
    assert r.headers["X-Trace-Id"]
    h = client.get(f"/api/v1/users/{student['user_id']}/attempts").json()
    assert len(h["attempts"]) == 1
    rev = client.get(f"/api/v1/attempts/{body['attempt_id']}", params={"user_id": student["user_id"]})
    assert rev.status_code == 200 and len(rev.json()["questions"]) == 5


def test_invalid_input(client, student, session_for):
    sid = session_for(student)
    r = put_attempt(client, sid, student["user_id"], answers={"0": 99})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = client.put(f"/api/v1/sessions/{sid}/attempt", json={"user_id": student["user_id"], "answers": {"x": "y"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert client.post("/api/v1/auth/register", json={"username": "a", "password": "x"}).status_code == 422
    # the failed attempts above must not have consumed the session
    assert put_attempt(client, sid, student["user_id"]).status_code == 201


def test_repeated_request_is_idempotent(client, student, session_for):
    sid = session_for(student)
    first = put_attempt(client, sid, student["user_id"])
    again = put_attempt(client, sid, student["user_id"])
    assert first.status_code == 201 and again.status_code == 200
    assert again.headers["Idempotent-Replay"] == "true"
    assert first.json() == again.json()
    assert len(client.get(f"/api/v1/users/{student['user_id']}/attempts").json()["attempts"]) == 1
    diff = put_attempt(client, sid, student["user_id"], answers={"0": 0})
    assert diff.status_code == 409


def test_ownership_and_missing(client, student, session_for):
    sid = session_for(student)
    other = client.post("/api/v1/auth/register", json={"username": "other_user1", "password": "secret123"})
    other = other.json() if other.status_code == 201 else {"user_id": "nobody"}
    assert put_attempt(client, sid, other["user_id"]).status_code == 403
    r = put_attempt(client, "missing", student["user_id"])
    assert r.status_code == 404
    assert set(r.json()["error"]) >= {"code", "message", "trace_id"}
    assert client.get("/api/v1/nope").json()["error"]["code"] == "not_found"


def test_server_side_tab_switch_counts(client, student, session_for):
    sid = session_for(student)
    t = client.post(f"/api/v1/sessions/{sid}/tab-switches", json={"user_id": student["user_id"]})
    assert t.status_code == 201 and t.json()["tab_switch_count"] == 1
    r = put_attempt(client, sid, student["user_id"], tab_switch_count=0)
    assert r.json()["status"] == "cheated" and r.json()["score"] == 0
    assert client.post(f"/api/v1/sessions/{sid}/tab-switches", json={"user_id": student["user_id"]}).status_code == 409


def test_login_rate_limit(client):
    codes = [client.post("/api/v1/auth/login", json={"username": "x", "password": "y"}) for _ in range(7)]
    assert [c.status_code for c in codes[:5]] == [401] * 5
    assert codes[5].status_code == 429 and codes[6].status_code == 429
    assert int(codes[5].headers["Retry-After"]) >= 1
    assert codes[5].json()["error"]["code"] == "rate_limited"


def test_metrics_exposed(client, student, session_for):
    put_attempt(client, session_for(student), student["user_id"])
    text = client.get("/metrics").text
    for name in ("veritas_http_requests_total", "veritas_http_request_duration_seconds_bucket",
                 "veritas_quiz_submissions_total", "veritas_active_test_takers", "veritas_rate_limited_total"):
        assert name in text
    assert 'route="/api/v1/sessions/{session_id}/attempt"' in text


def test_openapi_matches_committed_spec(client):
    from openapi_spec_validator import validate
    from app.main import app
    spec = app.openapi()
    validate(spec)
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "openapi", "openapi.json")
    assert json.load(open(path)) == json.loads(json.dumps(spec)), "run: python scripts/export_openapi.py"
