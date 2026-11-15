"""Business metrics shared by both processes (each process exposes its own
copy on its own /metrics endpoint; Prometheus adds the job label)."""
import os
from datetime import datetime, timedelta, timezone

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily

from app.core.db import get_db

QUIZ_SUBMISSIONS_TOTAL = Counter(
    "veritas_quiz_submissions_total",
    "Total number of quiz submissions, partitioned by outcome and quiz.",
    ["status", "quiz_id"],
)

GRADING_DURATION_SECONDS = Histogram(
    "veritas_grading_duration_seconds",
    "Time taken to auto-grade a submitted quiz, in seconds.",
    ["quiz_id"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0),
)

# NEW (A2): how often a retried submit was safely absorbed.
IDEMPOTENT_REPLAYS_TOTAL = Counter(
    "veritas_idempotent_replays_total",
    "Repeated submissions answered with the stored result instead of creating a duplicate.",
    ["operation"],
)

ACTIVE_SESSION_TTL_SECONDS = int(os.environ.get("ACTIVE_SESSION_TTL_SECONDS", "900"))


class DbStateCollector:
    """Scrape-time gauges derived from the shared DB (HTTP app only).

    Replaces A1's inc()/dec() gauges, which (a) drifted when students closed the
    tab, (b) break once start (HTTP) and submit (gRPC) run in different
    processes, and (c) reset to 0 on restart although the DB persists."""

    def describe(self):
        return []

    def collect(self):
        active = GaugeMetricFamily(
            "veritas_active_test_takers",
            "Open exam sessions (not submitted, younger than the TTL), per quiz.",
            labels=["quiz_id"])
        users = GaugeMetricFamily("veritas_registered_users_total",
                                  "Registered student accounts (from the database).")
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ACTIVE_SESSION_TTL_SECONDS)).isoformat()
            with get_db() as conn:
                for r in conn.execute(
                        """SELECT q.id AS quiz_id, COUNT(s.id) AS n FROM quizzes q
                           LEFT JOIN exam_sessions s ON s.quiz_id = q.id
                             AND s.ended_at IS NULL AND s.started_at > ?
                           GROUP BY q.id""", (cutoff,)):
                    active.add_metric([r["quiz_id"]], r["n"])
                users.add_metric([], conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0").fetchone()[0])
        except Exception:
            return  # never break /metrics because the DB is briefly unavailable
        yield active
        yield users
