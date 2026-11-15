"""Shared SQLite source of truth (file on a volume mounted into BOTH the
HTTP and gRPC containers). WAL mode + BEGIN IMMEDIATE for writers."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("VERITAS_DB_PATH", "/data/veritas.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_db(write: bool = False):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS quizzes (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, topic TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL, questions_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS exam_sessions (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, quiz_id TEXT NOT NULL,
        started_at TEXT NOT NULL, ended_at TEXT, attempt_id TEXT,
        tab_switch_count INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS attempts (
        id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL,
        quiz_id TEXT NOT NULL, score REAL NOT NULL, status TEXT NOT NULL,
        tab_switch_count INTEGER NOT NULL, correct INTEGER NOT NULL, total INTEGER NOT NULL,
        answers_json TEXT NOT NULL DEFAULT '{}', submitted_at TEXT NOT NULL)""",
]

SEED_QUIZZES = [
    {"id": "python_101", "title": "Python Fundamentals", "topic": "Python", "duration_seconds": 180,
     "questions": [
         {"q": "What does the 'len()' function return for a list?",
          "options": ["Its memory address", "The number of elements", "Its data type", "The last element"], "answer": 1},
         {"q": "Which keyword defines a function in Python?",
          "options": ["func", "def", "function", "lambda"], "answer": 1},
         {"q": "What is the output of `type([])`?",
          "options": ["<class 'list'>", "<class 'array'>", "<class 'tuple'>", "<class 'dict'>"], "answer": 0},
         {"q": "Which of these is immutable in Python?",
          "options": ["list", "dict", "tuple", "set"], "answer": 2},
         {"q": "What does 'pip' stand for (commonly)?",
          "options": ["Python Install Package", "Pip Installs Packages", "Package Index Program", "Python Internal Package"], "answer": 1},
     ]},
    {"id": "networks_201", "title": "Computer Networks Basics", "topic": "Networking", "duration_seconds": 180,
     "questions": [
         {"q": "Which layer of the OSI model handles routing?",
          "options": ["Physical", "Data Link", "Network", "Transport"], "answer": 2},
         {"q": "What does DNS resolve domain names into?",
          "options": ["MAC addresses", "IP addresses", "Port numbers", "URLs"], "answer": 1},
         {"q": "Which protocol is connection-oriented?",
          "options": ["UDP", "IP", "TCP", "ICMP"], "answer": 2},
         {"q": "What is the default port for HTTPS?",
          "options": ["80", "21", "443", "8080"], "answer": 2},
     ]},
    {"id": "observability_301", "title": "Observability Fundamentals", "topic": "SRE", "duration_seconds": 180,
     "questions": [
         {"q": "Which Prometheus metric type is monotonically increasing?",
          "options": ["Gauge", "Counter", "Summary", "Histogram"], "answer": 1},
         {"q": "What does p95 latency mean?",
          "options": ["95% of requests are slower than this", "95% of requests are faster than this", "The 95th request", "95% CPU usage"], "answer": 1},
         {"q": "Why avoid high-cardinality labels on Prometheus metrics?",
          "options": ["They are slower to type", "They cause a time-series explosion", "Prometheus rejects them", "They break Grafana colors"], "answer": 1},
         {"q": "What is the purpose of a trace ID in structured logs?",
          "options": ["To encrypt logs", "To correlate related events across a request", "To compress logs", "To timestamp logs"], "answer": 1},
     ]},
]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent; must run outside a transaction
    finally:
        conn.close()
    with get_db(write=True) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        if conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 0:
            for q in SEED_QUIZZES:
                conn.execute(
                    "INSERT INTO quizzes (id, title, topic, duration_seconds, questions_json) VALUES (?,?,?,?,?)",
                    (q["id"], q["title"], q["topic"], q["duration_seconds"], json.dumps(q["questions"])))
        if conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO users (id, username, password, is_admin, created_at) VALUES (?,?,?,1,?)",
                (str(uuid.uuid4()), "admin", "admin123", now_iso()))


def wait_for_db(timeout: float = 60.0):
    """gRPC service calls this: the HTTP app owns schema creation."""
    deadline = time.time() + timeout
    while True:
        try:
            with get_db() as conn:
                conn.execute("SELECT 1 FROM exam_sessions LIMIT 1")
            return
        except sqlite3.Error:
            if time.time() > deadline:
                raise
            time.sleep(1)
