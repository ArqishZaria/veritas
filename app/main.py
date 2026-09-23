import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Summary,
    Info,
    REGISTRY,
    generate_latest,
    CONTENT_TYPE_LATEST,
)


SERVICE_NAME = "veritas-app"


class ECSJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # `extra` fields are attached to the record by ecs_log() below.
        payload = {
            "@timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "log.level": getattr(record, "ecs_level", record.levelname.lower()),
            "message": record.getMessage(),
            "service.name": SERVICE_NAME,
            "trace.id": getattr(record, "trace_id", None),
            "event.action": getattr(record, "event_action", None),
            "event.duration": getattr(record, "event_duration", None),
            "student.id": getattr(record, "student_id", None),
        }
        # Merge any additional ECS-namespaced fields passed via extra=.
        extra_fields = getattr(record, "ecs_extra", None)
        if extra_fields:
            payload.update(extra_fields)
        # Drop keys with None values to keep the line lean.
        payload = {k: v for k, v in payload.items() if v is not None}
        return json.dumps(payload, separators=(",", ":"))


_logger = logging.getLogger("veritas")
_logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(stream=sys.stdout)
_handler.setFormatter(ECSJsonFormatter())
_logger.handlers = [_handler]
_logger.propagate = False


def new_trace_id() -> str:
    """W3C trace-context compliant 16-byte (32 hex char) trace id.

    A single uuid4().hex is exactly 32 hex characters (16 bytes) -- that
    alone satisfies the W3C traceparent trace-id field length. 
    """
    return uuid.uuid4().hex


def ecs_log(level: str, message: str, *, trace_id: str = None, event_action: str = None,
            event_duration_ns: int = None, student_id: str = None, **extra):
    level_map = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
    _logger.log(
        level_map.get(level, logging.INFO),
        message,
        extra={
            "ecs_level": level,
            "trace_id": trace_id,
            "event_action": event_action,
            "event_duration": event_duration_ns,
            "student_id": student_id,
            "ecs_extra": extra or None,
        },
    )



# 1. COUNTER -- monotonically increasing count of quiz outcomes.
QUIZ_SUBMISSIONS_TOTAL = Counter(
    "veritas_quiz_submissions_total",
    "Total number of quiz submissions, partitioned by outcome and quiz.",
    ["status", "quiz_id"],
)

# 2. GAUGE -- number of test-takers currently mid-exam, per quiz.
ACTIVE_TEST_TAKERS = Gauge(
    "veritas_active_test_takers",
    "Number of students currently taking a given quiz.",
    ["quiz_id"],
)

# 3. HISTOGRAM -- server-side auto-grading latency, bucketed for
#    histogram_quantile() based p95/p99 calculation in Grafana.
GRADING_DURATION_SECONDS = Histogram(
    "veritas_grading_duration_seconds",
    "Time taken to auto-grade a submitted quiz, in seconds.",
    ["quiz_id"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0),
)

# 4. SUMMARY -- end-to-end gateway processing time.
GATEWAY_PROCESSING_SECONDS = Summary(
    "veritas_gateway_processing_seconds",
    "End-to-end request processing time observed at the gateway layer.",
    ["route"],
)

APP_INFO = Info("veritas_app", "Static build metadata for the Veritas service.")
APP_INFO.info({"version": "1.0.0", "service": SERVICE_NAME})

REGISTERED_USERS_TOTAL = Gauge(
    "veritas_registered_users_total",
    "Total number of registered user accounts.",
)

CARDINALITY_DEMO_USE_LABEL = os.environ.get("CARDINALITY_DEMO_USE_LABEL", "true").lower() != "false"

if CARDINALITY_DEMO_USE_LABEL:
    DEMO_REQUESTS_TOTAL = Counter(
        "demo_requests_total",
        "Cardinality experiment counter (labeled by request_id -- anti-pattern).",
        ["request_id"],
    )
else:
    DEMO_REQUESTS_TOTAL = Counter(
        "demo_requests_total",
        "Cardinality experiment counter (no per-request label -- safe).",
    )



# ---------------------------------------------------------------------------
# DATABASE -- in-memory SQLite, seeded with demo quizzes
# ---------------------------------------------------------------------------

DB_PATH = "file:veritas_db?mode=memory&cache=shared"
_keepalive_conn = sqlite3.connect(DB_PATH, uri=True, check_same_thread=False)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                topic TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                questions_json TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                quiz_id TEXT NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL,
                tab_switch_count INTEGER NOT NULL,
                answers_json TEXT NOT NULL DEFAULT '{}',
                submitted_at TEXT NOT NULL
            )
        """)
        c.execute("SELECT COUNT(*) AS n FROM quizzes")
        if c.fetchone()["n"] == 0:
            seed_quizzes(conn)
        c.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1")
        if c.fetchone()["n"] == 0:
            seed_admin(conn)


def seed_admin(conn):
    c = conn.cursor()
    c.execute(
        "INSERT INTO users (id, username, password, is_admin, created_at) VALUES (?, ?, ?, 1, ?)",
        (str(uuid.uuid4()), "admin", "admin123", datetime.now(timezone.utc).isoformat()),
    )

def seed_quizzes(conn):
    quizzes = [
        {
            "id": "python_101",
            "title": "Python Fundamentals",
            "topic": "Python",
            "duration_seconds": 180,
            "questions": [
                {"q": "What does the 'len()' function return for a list?",
                 "options": ["Its memory address", "The number of elements", "Its data type", "The last element"],
                 "answer": 1},
                {"q": "Which keyword defines a function in Python?",
                 "options": ["func", "def", "function", "lambda"],
                 "answer": 1},
                {"q": "What is the output of `type([])`?",
                 "options": ["<class 'list'>", "<class 'array'>", "<class 'tuple'>", "<class 'dict'>"],
                 "answer": 0},
                {"q": "Which of these is immutable in Python?",
                 "options": ["list", "dict", "tuple", "set"],
                 "answer": 2},
                {"q": "What does 'pip' stand for (commonly)?",
                 "options": ["Python Install Package", "Pip Installs Packages", "Package Index Program", "Python Internal Package"],
                 "answer": 1},
            ],
        },
        {
            "id": "networks_201",
            "title": "Computer Networks Basics",
            "topic": "Networking",
            "duration_seconds": 180,
            "questions": [
                {"q": "Which layer of the OSI model handles routing?",
                 "options": ["Physical", "Data Link", "Network", "Transport"],
                 "answer": 2},
                {"q": "What does DNS resolve domain names into?",
                 "options": ["MAC addresses", "IP addresses", "Port numbers", "URLs"],
                 "answer": 1},
                {"q": "Which protocol is connection-oriented?",
                 "options": ["UDP", "IP", "TCP", "ICMP"],
                 "answer": 2},
                {"q": "What is the default port for HTTPS?",
                 "options": ["80", "21", "443", "8080"],
                 "answer": 2},
            ],
        },
        {
            "id": "observability_301",
            "title": "Observability Fundamentals",
            "topic": "SRE",
            "duration_seconds": 180,
            "questions": [
                {"q": "Which Prometheus metric type is monotonically increasing?",
                 "options": ["Gauge", "Counter", "Summary", "Histogram"],
                 "answer": 1},
                {"q": "What does p95 latency mean?",
                 "options": ["95% of requests are slower than this", "95% of requests are faster than this", "The 95th request", "95% CPU usage"],
                 "answer": 1},
                {"q": "Why avoid high-cardinality labels on Prometheus metrics?",
                 "options": ["They are slower to type", "They cause a time-series explosion", "Prometheus rejects them", "They break Grafana colors"],
                 "answer": 1},
                {"q": "What is the purpose of a trace ID in structured logs?",
                 "options": ["To encrypt logs", "To correlate related events across a request", "To compress logs", "To timestamp logs"],
                 "answer": 1},
            ],
        },
    ]
    c = conn.cursor()
    for q in quizzes:
        c.execute(
            "INSERT INTO quizzes (id, title, topic, duration_seconds, questions_json) VALUES (?, ?, ?, ?, ?)",
            (q["id"], q["title"], q["topic"], q["duration_seconds"], json.dumps(q["questions"])),
        )


init_db()

# ---------------------------------------------------------------------------
# APP SETUP
# ---------------------------------------------------------------------------

SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "true").lower() != "false"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SEED_DEMO_DATA:
        await seed_demo_data()
    yield


app = FastAPI(title="Veritas", version="1.0.0", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

_submission_counter = {"n": 0}  # used for the "every 5th submission" anomaly toggle


# ---------------------------------------------------------------------------
# MIDDLEWARE -- observes total request processing time at the gateway layer
# ---------------------------------------------------------------------------

@app.middleware("http")
async def gateway_timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    trace_id = new_trace_id()
    request.state.trace_id = trace_id
    response = await call_next(request)
    duration = time.perf_counter() - start
    route_obj = request.scope.get("route")
    route = route_obj.path if route_obj is not None else request.url.path
    GATEWAY_PROCESSING_SECONDS.labels(route=route).observe(duration)
    response.headers["X-Trace-Id"] = trace_id
    return response


# ---------------------------------------------------------------------------
# SCHEMAS
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class StartExamRequest(BaseModel):
    quiz_id: str
    user_id: str


class SubmitExamRequest(BaseModel):
    quiz_id: str
    user_id: str
    answers: dict  # {question_index (str): selected_option_index (int)}
    tab_switch_count: int = 0


class AdminQuizQuestion(BaseModel):
    q: str
    options: List[str]
    answer: int  # index into options


class AdminCreateQuizRequest(BaseModel):
    admin_id: str
    title: str
    topic: str
    duration_seconds: int = 180
    questions: List[AdminQuizQuestion]


# ---------------------------------------------------------------------------
# ADMIN AUTH HELPER
# ---------------------------------------------------------------------------

def require_admin(conn, user_id: str):
    c = conn.cursor()
    c.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if not row or not row["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")


# ---------------------------------------------------------------------------
# ROUTES -- FRONTEND
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ---------------------------------------------------------------------------
# ROUTES -- AUTH
# ---------------------------------------------------------------------------

@app.post("/api/register")
async def register(payload: RegisterRequest, request: Request):
    trace_id = request.state.trace_id
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username = ?", (payload.username,))
        if c.fetchone():
            ecs_log("warn", "Registration attempt with existing username",
                    trace_id=trace_id, event_action="register_failed")
            raise HTTPException(status_code=409, detail="Username already exists")
        user_id = str(uuid.uuid4())
        # NOTE: demo app only -- in production, hash with bcrypt/argon2.
        c.execute(
            "INSERT INTO users (id, username, password, created_at) VALUES (?, ?, ?, ?)",
            (user_id, payload.username, payload.password, datetime.now(timezone.utc).isoformat()),
        )
    REGISTERED_USERS_TOTAL.inc()
    ecs_log("info", "New user registered", trace_id=trace_id,
            event_action="user_registered", student_id=user_id)
    return {"user_id": user_id, "username": payload.username, "is_admin": False}


@app.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    trace_id = request.state.trace_id
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, username, is_admin FROM users WHERE username = ? AND password = ?",
            (payload.username, payload.password),
        )
        row = c.fetchone()
    if not row:
        ecs_log("warn", "Failed login attempt", trace_id=trace_id, event_action="login_failed")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    ecs_log("info", "User logged in", trace_id=trace_id,
            event_action="login_success", student_id=row["id"])
    return {"user_id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


# ---------------------------------------------------------------------------
# ROUTES -- QUIZ CATALOG & HISTORY
# ---------------------------------------------------------------------------

@app.get("/api/quizzes")
async def list_quizzes(search: Optional[str] = None):
    with get_db() as conn:
        c = conn.cursor()
        if search:
            c.execute(
                "SELECT id, title, topic, duration_seconds FROM quizzes WHERE title LIKE ? OR topic LIKE ?",
                (f"%{search}%", f"%{search}%"),
            )
        else:
            c.execute("SELECT id, title, topic, duration_seconds FROM quizzes")
        rows = [dict(r) for r in c.fetchall()]
    return {"quizzes": rows}


@app.get("/api/quizzes/{quiz_id}")
async def get_quiz(quiz_id: str):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quiz not found")
    questions = json.loads(row["questions_json"])
    # Strip correct answers before sending to the client.
    sanitized = [{"q": q["q"], "options": q["options"]} for q in questions]
    return {
        "id": row["id"],
        "title": row["title"],
        "topic": row["topic"],
        "duration_seconds": row["duration_seconds"],
        "questions": sanitized,
    }


@app.get("/api/history/{user_id}")
async def get_history(user_id: str):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT a.id, a.quiz_id, q.title, a.score, a.status, a.tab_switch_count, a.submitted_at
               FROM attempts a JOIN quizzes q ON a.quiz_id = q.id
               WHERE a.user_id = ? ORDER BY a.submitted_at DESC""",
            (user_id,),
        )
        rows = [dict(r) for r in c.fetchall()]
    avg_score = sum(r["score"] for r in rows) / len(rows) if rows else 0.0
    return {"attempts": rows, "average_score": round(avg_score, 2)}


@app.get("/api/attempts/{attempt_id}/review")
async def review_attempt(attempt_id: str, user_id: str):
    """Per-question breakdown for the 'View Answers' feature: what each
    question asked, what the student picked, and what the correct answer
    was. Requires the requesting user_id to own the attempt -- a student
    can only review their own attempts, not anyone else's, without needing
    a full session/token system (see require_admin's docstring for why
    this app uses user_id-as-proof-of-identity throughout)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT a.user_id, a.answers_json, a.score, a.status, a.tab_switch_count,
                      q.title, q.questions_json
               FROM attempts a JOIN quizzes q ON a.quiz_id = q.id
               WHERE a.id = ?""",
            (attempt_id,),
        )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You can only review your own attempts")

    questions = json.loads(row["questions_json"])
    submitted_answers = json.loads(row["answers_json"])
    breakdown = []
    for idx, q in enumerate(questions):
        selected = submitted_answers.get(str(idx))
        selected_idx = int(selected) if selected is not None else None
        breakdown.append({
            "question": q["q"],
            "options": q["options"],
            "correct_index": q["answer"],
            "selected_index": selected_idx,
            "is_correct": selected_idx == q["answer"],
        })
    return {
        "quiz_title": row["title"],
        "score": row["score"],
        "status": row["status"],
        "tab_switch_count": row["tab_switch_count"],
        "questions": breakdown,
    }


# ---------------------------------------------------------------------------
# ROUTES -- EXAM LIFECYCLE (anti-cheat + metrics + grading)
# ---------------------------------------------------------------------------

@app.post("/api/exam/start")
async def start_exam(payload: StartExamRequest, request: Request):
    trace_id = request.state.trace_id
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM quizzes WHERE id = ?", (payload.quiz_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="Quiz not found")

    ACTIVE_TEST_TAKERS.labels(quiz_id=payload.quiz_id).inc()
    ecs_log("info", "Student started exam", trace_id=trace_id,
            event_action="exam_started", student_id=payload.user_id,
            **{"quiz.id": payload.quiz_id})
    return {"status": "started", "trace_id": trace_id}


@app.post("/api/exam/tab_switch")
async def report_tab_switch(payload: dict, request: Request):
    """Called by the frontend's Page Visibility API listener every time the
    student leaves the exam tab/window."""
    trace_id = request.state.trace_id
    user_id = payload.get("user_id")
    quiz_id = payload.get("quiz_id")
    ecs_log("warn", "Tab switch detected during exam", trace_id=trace_id,
            event_action="tab_switched", student_id=user_id,
            **{"quiz.id": quiz_id})
    return {"status": "logged"}


async def _grade_and_record(quiz_id: str, user_id: str, answers: dict, tab_switch_count: int,
                             trace_id: str, simulate_anomaly: bool = False) -> dict:
    """Grading + persistence + metrics/logging, shared by the public
    /api/exam/submit endpoint and the startup demo-data seeder. Routing
    seed data through this exact function -- rather than inserting rows
    into `attempts` directly -- is what makes seeded activity show up in
    Prometheus and Kibana identically to real traffic, instead of only
    being visible in the app's own history table."""
    _submission_counter["n"] += 1
    is_scheduled_anomaly = _submission_counter["n"] % 5 == 0
    inject_delay = simulate_anomaly or is_scheduled_anomaly

    grading_start = time.perf_counter()
    if inject_delay:
        await asyncio.sleep(0.5)
        ecs_log("warn", "Injected artificial grading delay (anomaly experiment)",
                trace_id=trace_id, event_action="anomaly_injected",
                student_id=user_id, **{"quiz.id": quiz_id, "delay_ms": 500})

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT questions_json FROM quizzes WHERE id = ?", (quiz_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Quiz not found")
        questions = json.loads(row["questions_json"])

        # Auto-grade
        correct = 0
        for idx, q in enumerate(questions):
            selected = answers.get(str(idx))
            if selected is not None and int(selected) == q["answer"]:
                correct += 1
        raw_score = round((correct / len(questions)) * 100, 2) if questions else 0.0

        cheated = tab_switch_count > 0
        if cheated:
            final_score = 0.0
            status = "cheated"
        else:
            final_score = raw_score
            status = "passed" if raw_score >= 50 else "failed"

        attempt_id = str(uuid.uuid4())
        c.execute(
            """INSERT INTO attempts (id, user_id, quiz_id, score, status, tab_switch_count, answers_json, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt_id, user_id, quiz_id, final_score, status,
             tab_switch_count, json.dumps(answers), datetime.now(timezone.utc).isoformat()),
        )

    grading_duration = time.perf_counter() - grading_start
    GRADING_DURATION_SECONDS.labels(quiz_id=quiz_id).observe(grading_duration)
    QUIZ_SUBMISSIONS_TOTAL.labels(status=status, quiz_id=quiz_id).inc()
    ACTIVE_TEST_TAKERS.labels(quiz_id=quiz_id).dec()

    ecs_log(
        "warn" if cheated else "info",
        "Cheating detected: tab-switch penalty applied" if cheated else "Quiz submitted and auto-graded",
        trace_id=trace_id,
        event_action="quiz_submitted",
        event_duration_ns=int(grading_duration * 1_000_000_000),
        student_id=user_id,
        **{"quiz.id": quiz_id, "score": final_score, "status": status,
           "tab_switch_count": tab_switch_count},
    )

    return {
        "attempt_id": attempt_id,
        "score": final_score,
        "status": status,
        "cheated": cheated,
        "correct": correct,
        "total": len(questions),
    }


@app.post("/api/exam/submit")
async def submit_exam(payload: SubmitExamRequest, request: Request):
    trace_id = request.state.trace_id
    simulate_anomaly = request.query_params.get("simulate_anomaly", "false").lower() == "true"
    return await _grade_and_record(
        payload.quiz_id, payload.user_id, payload.answers,
        payload.tab_switch_count, trace_id, simulate_anomaly,
    )



DEMO_STUDENTS = ["alice_demo", "bob_demo", "carol_demo", "dave_demo", "erin_demo"]
DEMO_PASSWORD = "demo123"

DEMO_ATTEMPTS = [
    ("alice_demo", "python_101",        {"0": 1, "1": 1, "2": 0, "3": 2, "4": 1}, 0),  # 5/5 passed
    ("alice_demo", "observability_301", {"0": 1, "1": 1, "2": 1, "3": 1}, 0),          # 4/4 passed
    ("alice_demo", "networks_201",      {"0": 2, "1": 1, "2": 2, "3": 9}, 0),          # 3/4 passed
    ("bob_demo",   "python_101",        {"0": 1, "1": 1, "2": 0, "3": 9, "4": 9}, 0),  # 3/5 passed
    ("bob_demo",   "networks_201",      {"0": 2, "1": 1, "2": 2, "3": 2}, 1),          # cheated
    ("bob_demo",   "observability_301", {"0": 1, "1": 1, "2": 9, "3": 9}, 0),          # 2/4 passed
    ("carol_demo", "networks_201",      {"0": 9, "1": 9, "2": 2, "3": 9}, 0),          # 1/4 failed
    ("carol_demo", "observability_301", {"0": 9, "1": 9, "2": 9, "3": 1}, 0),          # 1/4 failed
    ("dave_demo",  "python_101",        {"0": 9, "1": 9, "2": 9, "3": 9, "4": 9}, 0),  # 0/5 failed
    ("dave_demo",  "networks_201",      {"0": 2, "1": 1, "2": 2, "3": 2}, 0),          # 4/4 passed
    ("erin_demo",  "observability_301", {"0": 1, "1": 1, "2": 1, "3": 1}, 2),          # cheated
    ("erin_demo",  "python_101",        {"0": 1, "1": 1, "2": 0, "3": 2, "4": 9}, 0),  # 4/5 passed
]


async def seed_demo_data():
    trace_id = new_trace_id()

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM users WHERE username = ?", (DEMO_STUDENTS[0],))
        if c.fetchone()["n"] > 0:
            return  # already seeded this run (shouldn't happen with a fresh in-memory DB, but cheap to guard)

        user_ids = {}
        for name in DEMO_STUDENTS:
            uid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO users (id, username, password, is_admin, created_at) VALUES (?, ?, ?, 0, ?)",
                (uid, name, DEMO_PASSWORD, datetime.now(timezone.utc).isoformat()),
            )
            user_ids[name] = uid
    REGISTERED_USERS_TOTAL.inc(len(DEMO_STUDENTS))
    ecs_log("info", "Seeded demo student accounts", trace_id=trace_id,
            event_action="demo_seed_users", **{"count": len(DEMO_STUDENTS)})

    for username, quiz_id, answers, tab_switches in DEMO_ATTEMPTS:
        uid = user_ids[username]
        ACTIVE_TEST_TAKERS.labels(quiz_id=quiz_id).inc()
        ecs_log("info", "Demo seed: exam started", trace_id=trace_id,
                event_action="exam_started", student_id=uid, **{"quiz.id": quiz_id})
        for _ in range(tab_switches):
            ecs_log("warn", "Demo seed: tab switch detected during exam", trace_id=trace_id,
                    event_action="tab_switched", student_id=uid, **{"quiz.id": quiz_id})
        await _grade_and_record(quiz_id, uid, answers, tab_switches, trace_id)

    ecs_log("info", "Demo data seeding complete", trace_id=trace_id,
            event_action="demo_seed_complete", **{"attempts": len(DEMO_ATTEMPTS)})


# ---------------------------------------------------------------------------
# ROUTES -- ADMIN
# ---------------------------------------------------------------------------

@app.get("/api/admin/stats")
async def admin_stats(admin_id: str):
    with get_db() as conn:
        require_admin(conn, admin_id)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 0")
        total_users = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM quizzes")
        total_quizzes = c.fetchone()["n"]
        c.execute("SELECT status, COUNT(*) AS n FROM attempts GROUP BY status")
        by_status = {row["status"]: row["n"] for row in c.fetchall()}
        c.execute("SELECT COUNT(*) AS n FROM attempts")
        total_attempts = c.fetchone()["n"]
    cheated = by_status.get("cheated", 0)
    cheating_rate = round(100 * cheated / total_attempts, 2) if total_attempts else 0.0
    return {
        "total_users": total_users,
        "total_quizzes": total_quizzes,
        "total_attempts": total_attempts,
        "by_status": by_status,
        "cheating_rate": cheating_rate,
    }


@app.get("/api/admin/attempts")
async def admin_all_attempts(admin_id: str):
    with get_db() as conn:
        require_admin(conn, admin_id)
        c = conn.cursor()
        c.execute(
            """SELECT a.id, u.username, q.title AS quiz_title, a.score, a.status,
                      a.tab_switch_count, a.submitted_at
               FROM attempts a
               JOIN users u ON a.user_id = u.id
               JOIN quizzes q ON a.quiz_id = q.id
               ORDER BY a.submitted_at DESC"""
        )
        rows = [dict(r) for r in c.fetchall()]
    return {"attempts": rows}


@app.get("/api/admin/quizzes")
async def admin_list_quizzes(admin_id: str):
    """Like /api/quizzes, but includes correct answers -- for admin
    management only. Never exposed on the public /api/quizzes* routes."""
    with get_db() as conn:
        require_admin(conn, admin_id)
        c = conn.cursor()
        c.execute("SELECT id, title, topic, duration_seconds, questions_json FROM quizzes")
        rows = []
        for r in c.fetchall():
            row = dict(r)
            row["questions"] = json.loads(row.pop("questions_json"))
            rows.append(row)
    return {"quizzes": rows}


@app.post("/api/admin/quizzes")
async def admin_create_quiz(payload: AdminCreateQuizRequest, request: Request):
    trace_id = request.state.trace_id
    if not payload.questions:
        raise HTTPException(status_code=400, detail="A quiz needs at least one question")
    for q in payload.questions:
        if not (0 <= q.answer < len(q.options)):
            raise HTTPException(status_code=400, detail=f"Invalid answer index for question: {q.q!r}")

    quiz_id = str(uuid.uuid4())
    with get_db() as conn:
        require_admin(conn, payload.admin_id)
        c = conn.cursor()
        c.execute(
            "INSERT INTO quizzes (id, title, topic, duration_seconds, questions_json) VALUES (?, ?, ?, ?, ?)",
            (quiz_id, payload.title, payload.topic, payload.duration_seconds,
             json.dumps([q.model_dump() for q in payload.questions])),
        )
    ecs_log("info", "Admin created a new quiz", trace_id=trace_id,
            event_action="admin_quiz_created", student_id=payload.admin_id,
            **{"quiz.id": quiz_id, "quiz.title": payload.title})
    return {"id": quiz_id, "title": payload.title, "topic": payload.topic,
            "duration_seconds": payload.duration_seconds}


@app.delete("/api/admin/quizzes/{quiz_id}")
async def admin_delete_quiz(quiz_id: str, admin_id: str, request: Request):
    trace_id = request.state.trace_id
    with get_db() as conn:
        require_admin(conn, admin_id)
        c = conn.cursor()
        c.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
        deleted = c.rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Quiz not found")
    ecs_log("info", "Admin deleted a quiz", trace_id=trace_id,
            event_action="admin_quiz_deleted", student_id=admin_id, **{"quiz.id": quiz_id})
    return {"status": "deleted", "id": quiz_id}


# ---------------------------------------------------------------------------
# ROUTES -- EXPERIMENTS (Part E.2: cardinality explosion, hands-on)
# ---------------------------------------------------------------------------

@app.post("/api/experiments/cardinality")
async def run_cardinality_experiment(count: int = 20):
    """Generates `count` calls against demo_requests_total, exactly as
    Part E.2 asks for. With CARDINALITY_DEMO_USE_LABEL=true (the default),
    each call gets its own request_id label -- run this with count=100,
    then check `count(demo_requests_total)` in Prometheus and watch it
    read ~100. Flip CARDINALITY_DEMO_USE_LABEL=false in docker-compose.yml,
    restart the veritas container, and call this again with the same
    count=100 -- `count(demo_requests_total)` will now read 1, since every
    call increments the same unlabeled series instead of creating new ones.
    """
    count = max(1, min(count, 500))  # sane upper bound so nobody nukes their own Prometheus by accident
    generated_ids = []
    for _ in range(count):
        if CARDINALITY_DEMO_USE_LABEL:
            rid = str(uuid.uuid4())
            DEMO_REQUESTS_TOTAL.labels(request_id=rid).inc()
            generated_ids.append(rid)
        else:
            DEMO_REQUESTS_TOTAL.inc()
    return {
        "calls_made": count,
        "using_request_id_label": CARDINALITY_DEMO_USE_LABEL,
        "new_series_created_by_this_call": count if CARDINALITY_DEMO_USE_LABEL else 0,
        "sample_request_ids": generated_ids[:3] if generated_ids else [],
    }


# ---------------------------------------------------------------------------
# ROUTES -- OBSERVABILITY
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}
