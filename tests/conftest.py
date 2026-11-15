import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp()
os.environ.update(VERITAS_DB_PATH=os.path.join(_tmp, "test.db"), SEED_DEMO_DATA="false",
                  ENABLE_FAULT_INJECTION="true", LOGIN_RATE_LIMIT="5", SERVICE_NAME="veritas-test")
GEN = os.path.join(ROOT, "gen")
if not os.path.exists(os.path.join(GEN, "veritas/exam/v1/exam_pb2.py")):
    os.makedirs(GEN, exist_ok=True)
    subprocess.check_call([sys.executable, "-m", "grpc_tools.protoc", "-I", os.path.join(ROOT, "proto"),
                           f"--python_out={GEN}", f"--grpc_python_out={GEN}",
                           os.path.join(ROOT, "proto/veritas/exam/v1/exam.proto")])
for p in (ROOT, GEN):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def student(client):
    import uuid
    name = f"stu_{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/auth/register", json={"username": name, "password": "secret123"})
    assert r.status_code == 201
    return r.json()


@pytest.fixture()
def session_for(client):
    def _make(user, quiz="python_101"):
        r = client.post(f"/api/v1/quizzes/{quiz}/sessions", json={"user_id": user["user_id"]})
        assert r.status_code == 201, r.text
        return r.json()["session_id"]
    return _make
