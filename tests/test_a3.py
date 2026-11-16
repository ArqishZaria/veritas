import time
import uuid

from app.ratelimit import DbSlidingWindowLimiter


def test_replica_id_in_header_and_health(client):
    assert client.get("/health").json()["replica"] == "test"
    assert client.get("/api/v1/quizzes/python_101").headers["X-Replica-Id"] == "test"


def test_rate_limit_counters_are_shared_between_instances(client):
    """Two limiter objects = two replicas: they must count against the same budget (shared DB)."""
    a, b = DbSlidingWindowLimiter(2, 60), DbSlidingWindowLimiter(2, 60)
    key = uuid.uuid4().hex
    assert a.check(key) == (True, 0)
    assert b.check(key) == (True, 0)
    ok, retry = a.check(key)
    assert not ok and 1 <= retry <= 60
    assert b.check(key)[0] is False


def test_delay_hook_delays_api_but_not_health(client):
    assert client.put("/internal/delay", params={"delay_ms": 150}).json()["delay_ms"] == 150
    try:
        t = time.perf_counter()
        client.get("/api/v1/quizzes/python_101")
        assert time.perf_counter() - t >= 0.14
        t = time.perf_counter()
        client.get("/health")  # a slow replica still passes health checks
        assert time.perf_counter() - t < 0.1
    finally:
        client.put("/internal/delay", params={"delay_ms": 0})
