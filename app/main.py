import asyncio
import json
import os
import sqlite3
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, Info,
                               Summary, generate_latest)

from app import errors
from app.core import chaos, exam, metrics
from app.core.db import get_db, init_db, now_iso
from app.core.logging_ecs import REPLICA_ID, SERVICE_NAME, ecs_log, is_valid_trace_id, new_trace_id
from app.errors import ApiError, error_body, from_domain
from app.ratelimit import DbSlidingWindowLimiter, SlidingWindowLimiter

SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "true").lower() != "false"
ENABLE_FAULT_INJECTION = os.environ.get("ENABLE_FAULT_INJECTION", "false").lower() == "true"
CARDINALITY_DEMO_USE_LABEL = os.environ.get("CARDINALITY_DEMO_USE_LABEL", "false").lower() == "true"
LOGIN_RATE_LIMIT = int(os.environ.get("LOGIN_RATE_LIMIT", "5"))
LOGIN_RATE_WINDOW = float(os.environ.get("LOGIN_RATE_WINDOW", "60"))
ENABLE_GRPC = os.environ.get("ENABLE_GRPC", "false").lower() == "true"      # A3: gRPC server inside the same process
RATE_LIMIT_BACKEND = os.environ.get("RATE_LIMIT_BACKEND", "db").lower()      # A3: "db" (shared) | "memory" (per replica)

# ---------------------------------------------------------------------------
# METRICS (HTTP layer). Business metrics live in app/core/metrics.py.
# ---------------------------------------------------------------------------
GATEWAY_PROCESSING_SECONDS = Summary(
    "veritas_gateway_processing_seconds",
    "End-to-end request processing time observed at the gateway layer.", ["route"])
HTTP_REQUESTS_TOTAL = Counter(
    "veritas_http_requests_total", "HTTP requests by method, route template and status code.",
    ["method", "route", "status"])
HTTP_DURATION_SECONDS = Histogram(
    "veritas_http_request_duration_seconds", "HTTP request duration by method and route template.",
    ["method", "route"], buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0))
HTTP_IN_FLIGHT = Gauge("veritas_http_requests_in_flight", "HTTP requests currently being handled.")
RATE_LIMITED_TOTAL = Counter("veritas_rate_limited_total", "Requests rejected with 429.", ["endpoint"])
APP_INFO = Info("veritas_app", "Static build metadata for the Veritas service.")
APP_INFO.info({"version": "2.0.0", "service": SERVICE_NAME, "replica": REPLICA_ID})
REGISTRY.register(metrics.DbStateCollector())

if CARDINALITY_DEMO_USE_LABEL:
    DEMO_REQUESTS_TOTAL = Counter("demo_requests_total",
                                  "Cardinality experiment (labeled by request_id -- anti-pattern).", ["request_id"])
else:
    DEMO_REQUESTS_TOTAL = Counter("demo_requests_total", "Cardinality experiment (no per-request label -- safe).")

# ---------------------------------------------------------------------------
# SCHEMAS
# ---------------------------------------------------------------------------
Status = Literal["passed", "failed", "cheated"]


class ErrorBody(BaseModel):
    code: str
    message: str
    trace_id: Optional[str] = None
    details: Optional[List[Dict[str, str]]] = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    user_id: str
    username: str
    is_admin: bool


class QuizSummary(BaseModel):
    id: str
    title: str
    topic: str
    duration_seconds: int


class QuizListOut(BaseModel):
    quizzes: List[QuizSummary]


class QuizQuestionPublic(BaseModel):
    q: str
    options: List[str]


class QuizDetail(QuizSummary):
    questions: List[QuizQuestionPublic]


class SessionCreate(BaseModel):
    user_id: str = Field(min_length=1)


class SessionOut(BaseModel):
    session_id: str
    quiz_id: str
    started_at: str


class TabSwitchCreate(BaseModel):
    user_id: str = Field(min_length=1)


class TabSwitchOut(BaseModel):
    tab_switch_count: int


class SubmitAttemptRequest(BaseModel):
    user_id: str = Field(min_length=1)
    answers: Dict[int, int] = Field(default_factory=dict, description="question index -> chosen option index")
    tab_switch_count: int = Field(0, ge=0, le=1000)


class AttemptOut(BaseModel):
    attempt_id: str
    score: float
    status: Status
    cheated: bool
    correct: int
    total: int


class HistoryItem(BaseModel):
    id: str
    quiz_id: str
    title: str
    score: float
    status: Status
    tab_switch_count: int
    submitted_at: str


class HistoryOut(BaseModel):
    attempts: List[HistoryItem]
    average_score: float


class ReviewQuestion(BaseModel):
    question: str
    options: List[str]
    correct_index: int
    selected_index: Optional[int] = None
    is_correct: bool


class ReviewOut(BaseModel):
    quiz_title: str
    score: float
    status: Status
    tab_switch_count: int
    questions: List[ReviewQuestion]


class AdminStats(BaseModel):
    total_users: int
    total_quizzes: int
    total_attempts: int
    by_status: Dict[str, int]
    cheating_rate: float


class AdminAttemptRow(BaseModel):
    id: str
    username: str
    quiz_title: str
    score: float
    status: Status
    tab_switch_count: int
    submitted_at: str


class AdminAttemptsOut(BaseModel):
    attempts: List[AdminAttemptRow]


class AdminQuizQuestion(BaseModel):
    q: str = Field(min_length=1, max_length=500)
    options: List[str] = Field(min_length=2, max_length=6)
    answer: int = Field(ge=0)

    @model_validator(mode="after")
    def _answer_in_range(self):
        if self.answer >= len(self.options):
            raise ValueError("answer must be a valid index into options")
        if any(not o.strip() for o in self.options):
            raise ValueError("options must not be blank")
        return self


class AdminQuiz(QuizSummary):
    questions: List[AdminQuizQuestion]


class AdminQuizzesOut(BaseModel):
    quizzes: List[AdminQuiz]


class AdminCreateQuizRequest(BaseModel):
    admin_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    topic: str = Field(min_length=1, max_length=60)
    duration_seconds: int = Field(180, ge=30, le=7200)
    questions: List[AdminQuizQuestion] = Field(min_length=1, max_length=100)


class CardinalityOut(BaseModel):
    calls_made: int
    using_request_id_label: bool
    new_series_created_by_this_call: int
    sample_request_ids: List[str]


class FaultRequest(BaseModel):
    admin_id: str
    mode: Literal["off", "error", "slow"] = "off"
    remaining: int = Field(0, ge=0, le=1000)
    delay_seconds: float = Field(0, ge=0, le=30)


_ERR_DESC = {400: "Bad request", 401: "Invalid credentials", 403: "Not allowed to access this resource",
             404: "Resource not found", 409: "Conflict with current state",
             422: "Validation failed", 429: "Rate limit exceeded", 503: "Service temporarily unavailable"}


def errs(*codes):
    out = {}
    for c in codes:
        out[c] = {"model": ErrorEnvelope, "description": _ERR_DESC[c]}
        if c == 429:
            out[c]["headers"] = {"Retry-After": {
                "description": "Seconds until the caller may retry", "schema": {"type": "integer"}}}
    return out


# ---------------------------------------------------------------------------
# APP + MIDDLEWARE
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    if SEED_DEMO_DATA:
        await asyncio.to_thread(seed_demo_data)
    grpc_server = None
    if ENABLE_GRPC:  # A3: every replica serves HTTP (uvicorn) and gRPC (same process, same metrics registry)
        from grpc_service.server import build_server
        grpc_server, grpc_port = build_server()
        grpc_server.start()
        ecs_log("info", f"gRPC ExamService listening on :{grpc_port}", event_action="grpc_started")
    yield
    if grpc_server is not None:
        grpc_server.stop(5)


app = FastAPI(title="Veritas API", version="2.0.0", lifespan=lifespan,
              description="Online assessment platform. All errors use {\"error\": {code, message, trace_id, details?}}.")
errors.install(app)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
login_limiter = (SlidingWindowLimiter if RATE_LIMIT_BACKEND == "memory" else DbSlidingWindowLimiter)(
    LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW)
FAULT = {"mode": "off", "remaining": 0, "delay_seconds": 0.0}


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    start = time.perf_counter()
    incoming = request.headers.get("x-trace-id", "")
    trace_id = incoming if is_valid_trace_id(incoming) else new_trace_id()
    request.state.trace_id = trace_id
    tracked = request.url.path not in ("/metrics", "/health")
    if tracked:
        HTTP_IN_FLIGHT.inc()
    try:
        try:
            if tracked and not request.url.path.startswith("/internal"):
                delay = chaos.get_delay_s()  # A3 experiment hook; /health and /metrics are never delayed
                if delay:
                    await asyncio.sleep(delay)
            response = await call_next(request)
        except Exception:
            ecs_log("error", "Unhandled exception", trace_id=trace_id, event_action="unhandled_exception",
                    **{"error.stack_trace": traceback.format_exc()})
            response = JSONResponse(status_code=500, content=error_body(
                request, "internal_error", "Internal server error"))
    finally:
        if tracked:
            HTTP_IN_FLIGHT.dec()
    duration = time.perf_counter() - start
    route_obj = request.scope.get("route")
    route = route_obj.path if route_obj is not None else "unmatched"  # bounded label
    GATEWAY_PROCESSING_SECONDS.labels(route=route).observe(duration)
    if tracked:
        HTTP_REQUESTS_TOTAL.labels(method=request.method, route=route, status=str(response.status_code)).inc()
        HTTP_DURATION_SECONDS.labels(method=request.method, route=route).observe(duration)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Replica-Id"] = REPLICA_ID
    return response


def login_rate_limit(request: Request):
    key = request.client.host if request.client else "unknown"
    allowed, retry_after = login_limiter.check(key)
    if not allowed:
        RATE_LIMITED_TOTAL.labels(endpoint="login").inc()
        ecs_log("warn", "Login rate limit exceeded", trace_id=request.state.trace_id,
                event_action="rate_limited", **{"client.ip": key, "retry_after_s": retry_after})
        raise ApiError(429, "rate_limited", f"Too many login attempts. Retry in {retry_after}s.",
                       headers={"Retry-After": str(retry_after)})


def require_admin(conn, user_id: str):
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["is_admin"]:
        raise ApiError(403, "forbidden", "Admin access required")


router = APIRouter(prefix="/api/v1")


# ------------------------------- AUTH --------------------------------------
@router.post("/auth/register", response_model=UserOut, status_code=201, tags=["auth"],
             summary="Register a student account", responses=errs(409, 422))
def register(payload: RegisterRequest, request: Request):
    trace_id = request.state.trace_id
    user_id = str(uuid.uuid4())
    with get_db(write=True) as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (payload.username,)).fetchone():
            ecs_log("warn", "Registration attempt with existing username", trace_id=trace_id,
                    event_action="register_failed")
            raise ApiError(409, "username_taken", "Username already exists")
        # NOTE: demo app only -- hash with bcrypt/argon2 in production.
        conn.execute("INSERT INTO users (id, username, password, created_at) VALUES (?,?,?,?)",
                     (user_id, payload.username, payload.password, now_iso()))
    ecs_log("info", "New user registered", trace_id=trace_id, event_action="user_registered", student_id=user_id)
    return {"user_id": user_id, "username": payload.username, "is_admin": False}


@router.post("/auth/login", response_model=UserOut, tags=["auth"], summary="Log in (rate limited per client IP)",
             dependencies=[Depends(login_rate_limit)], responses=errs(401, 422, 429))
def login(payload: LoginRequest, request: Request):
    trace_id = request.state.trace_id
    with get_db() as conn:
        row = conn.execute("SELECT id, username, is_admin FROM users WHERE username = ? AND password = ?",
                           (payload.username, payload.password)).fetchone()
    if not row:
        ecs_log("warn", "Failed login attempt", trace_id=trace_id, event_action="login_failed")
        raise ApiError(401, "invalid_credentials", "Invalid credentials")
    ecs_log("info", "User logged in", trace_id=trace_id, event_action="login_success", student_id=row["id"])
    return {"user_id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


# ------------------------------- QUIZZES -----------------------------------
@router.get("/quizzes", response_model=QuizListOut, tags=["quizzes"], summary="List/search quizzes",
            responses=errs(422))
def list_quizzes(search: Optional[str] = Query(None, max_length=100)):
    with get_db() as conn:
        if search:
            rows = conn.execute("SELECT id, title, topic, duration_seconds FROM quizzes "
                                "WHERE title LIKE ? OR topic LIKE ?", (f"%{search}%", f"%{search}%")).fetchall()
        else:
            rows = conn.execute("SELECT id, title, topic, duration_seconds FROM quizzes").fetchall()
    return {"quizzes": [dict(r) for r in rows]}


@router.get("/quizzes/{quiz_id}", response_model=QuizDetail, tags=["quizzes"],
            summary="Get a quiz (correct answers are never included)", responses=errs(404))
def get_quiz(quiz_id: str, request: Request):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if not row:
        raise ApiError(404, "not_found", "Quiz not found")
    qs = [{"q": q["q"], "options": q["options"]} for q in json.loads(row["questions_json"])]
    ecs_log("info", "Quiz served", trace_id=request.state.trace_id, event_action="quiz_served", **{"quiz.id": quiz_id})
    return {"id": row["id"], "title": row["title"], "topic": row["topic"],
            "duration_seconds": row["duration_seconds"], "questions": qs}


# ------------------------------- EXAM LIFECYCLE ----------------------------
@router.post("/quizzes/{quiz_id}/sessions", response_model=SessionOut, status_code=201, tags=["exam"],
             summary="Start an exam session", responses=errs(404, 422))
def start_session(quiz_id: str, payload: SessionCreate, request: Request, response: Response):
    try:
        out = exam.start_session(payload.user_id, quiz_id, request.state.trace_id)
    except exam.DomainError as e:
        raise from_domain(e)
    response.headers["Location"] = f"/api/v1/sessions/{out['session_id']}"
    return out


@router.post("/sessions/{session_id}/tab-switches", response_model=TabSwitchOut, status_code=201, tags=["exam"],
             summary="Record that the student left the exam window", responses=errs(403, 404, 409, 422))
def add_tab_switch(session_id: str, payload: TabSwitchCreate, request: Request):
    try:
        return {"tab_switch_count": exam.record_tab_switch(session_id, payload.user_id, request.state.trace_id)}
    except exam.DomainError as e:
        raise from_domain(e)


async def _maybe_inject_fault():
    if ENABLE_FAULT_INJECTION and FAULT["remaining"] > 0 and FAULT["mode"] != "off":
        FAULT["remaining"] -= 1
        if FAULT["mode"] == "error":
            raise ApiError(503, "unavailable", "Injected failure (fault-injection experiment)")
        await asyncio.sleep(FAULT["delay_seconds"])


@router.put("/sessions/{session_id}/attempt", response_model=AttemptOut, status_code=201, tags=["exam"],
            summary="Submit answers (idempotent: safe to retry)",
            description="The session id is the idempotency key. First call -> 201 Created. Repeating with the "
                        "same answers -> 200 OK with the stored result and `Idempotent-Replay: true`. "
                        "Repeating with different answers -> 409.",
            responses={200: {"model": AttemptOut, "description": "Replay of an already-recorded submission",
                             "headers": {"Idempotent-Replay": {"schema": {"type": "string"}}}},
                       **errs(403, 404, 409, 422, 503)})
async def submit_attempt(session_id: str, payload: SubmitAttemptRequest, request: Request, response: Response,
                         simulate_anomaly: bool = Query(False)):
    await _maybe_inject_fault()
    try:
        result, replayed = await run_in_threadpool(
            exam.submit_attempt, session_id, payload.user_id, payload.answers,
            payload.tab_switch_count, simulate_anomaly, request.state.trace_id)
    except exam.DomainError as e:
        raise from_domain(e)
    if replayed:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return result


# ------------------------------- HISTORY / REVIEW --------------------------
@router.get("/users/{user_id}/attempts", response_model=HistoryOut, tags=["attempts"],
            summary="A student's attempt history", responses=errs(404))
def get_history(user_id: str):
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise ApiError(404, "not_found", "User not found")
        rows = [dict(r) for r in conn.execute(
            """SELECT a.id, a.quiz_id, q.title, a.score, a.status, a.tab_switch_count, a.submitted_at
               FROM attempts a JOIN quizzes q ON a.quiz_id = q.id
               WHERE a.user_id = ? ORDER BY a.submitted_at DESC""", (user_id,))]
    avg = sum(r["score"] for r in rows) / len(rows) if rows else 0.0
    return {"attempts": rows, "average_score": round(avg, 2)}


@router.get("/attempts/{attempt_id}", response_model=ReviewOut, tags=["attempts"],
            summary="Per-question review of one's own attempt", responses=errs(403, 404, 422))
def review_attempt(attempt_id: str, user_id: str):
    with get_db() as conn:
        row = conn.execute(
            """SELECT a.user_id, a.answers_json, a.score, a.status, a.tab_switch_count, q.title, q.questions_json
               FROM attempts a JOIN quizzes q ON a.quiz_id = q.id WHERE a.id = ?""", (attempt_id,)).fetchone()
    if not row:
        raise ApiError(404, "not_found", "Attempt not found")
    if row["user_id"] != user_id:
        raise ApiError(403, "forbidden", "You can only review your own attempts")
    submitted = json.loads(row["answers_json"])
    breakdown = []
    for idx, q in enumerate(json.loads(row["questions_json"])):
        sel = submitted.get(str(idx))
        sel = int(sel) if sel is not None else None
        breakdown.append({"question": q["q"], "options": q["options"], "correct_index": q["answer"],
                          "selected_index": sel, "is_correct": sel == q["answer"]})
    return {"quiz_title": row["title"], "score": row["score"], "status": row["status"],
            "tab_switch_count": row["tab_switch_count"], "questions": breakdown}


# ------------------------------- ADMIN -------------------------------------
@router.get("/admin/stats", response_model=AdminStats, tags=["admin"], summary="Platform totals",
            responses=errs(403, 422))
def admin_stats(admin_id: str):
    with get_db() as conn:
        require_admin(conn, admin_id)
        users = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0").fetchone()[0]
        quizzes = conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0]
        by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM attempts GROUP BY status")}
        total = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    rate = round(100 * by_status.get("cheated", 0) / total, 2) if total else 0.0
    return {"total_users": users, "total_quizzes": quizzes, "total_attempts": total,
            "by_status": by_status, "cheating_rate": rate}


@router.get("/admin/attempts", response_model=AdminAttemptsOut, tags=["admin"], summary="All attempts",
            responses=errs(403, 422))
def admin_all_attempts(admin_id: str):
    with get_db() as conn:
        require_admin(conn, admin_id)
        rows = [dict(r) for r in conn.execute(
            """SELECT a.id, u.username, q.title AS quiz_title, a.score, a.status, a.tab_switch_count, a.submitted_at
               FROM attempts a JOIN users u ON a.user_id = u.id JOIN quizzes q ON a.quiz_id = q.id
               ORDER BY a.submitted_at DESC""")]
    return {"attempts": rows}


@router.get("/admin/quizzes", response_model=AdminQuizzesOut, tags=["admin"],
            summary="List quizzes including correct answers", responses=errs(403, 422))
def admin_list_quizzes(admin_id: str):
    with get_db() as conn:
        require_admin(conn, admin_id)
        rows = []
        for r in conn.execute("SELECT id, title, topic, duration_seconds, questions_json FROM quizzes"):
            d = dict(r)
            d["questions"] = json.loads(d.pop("questions_json"))
            rows.append(d)
    return {"quizzes": rows}


@router.post("/admin/quizzes", response_model=QuizSummary, status_code=201, tags=["admin"],
             summary="Create a quiz", responses=errs(403, 422))
def admin_create_quiz(payload: AdminCreateQuizRequest, request: Request, response: Response):
    quiz_id = str(uuid.uuid4())
    with get_db(write=True) as conn:
        require_admin(conn, payload.admin_id)
        conn.execute("INSERT INTO quizzes (id, title, topic, duration_seconds, questions_json) VALUES (?,?,?,?,?)",
                     (quiz_id, payload.title, payload.topic, payload.duration_seconds,
                      json.dumps([q.model_dump() for q in payload.questions])))
    ecs_log("info", "Admin created a new quiz", trace_id=request.state.trace_id, event_action="admin_quiz_created",
            student_id=payload.admin_id, **{"quiz.id": quiz_id})
    response.headers["Location"] = f"/api/v1/quizzes/{quiz_id}"
    return {"id": quiz_id, "title": payload.title, "topic": payload.topic, "duration_seconds": payload.duration_seconds}


@router.delete("/admin/quizzes/{quiz_id}", status_code=204, tags=["admin"], summary="Delete a quiz",
               responses=errs(403, 404, 422))
def admin_delete_quiz(quiz_id: str, admin_id: str, request: Request):
    with get_db(write=True) as conn:
        require_admin(conn, admin_id)
        deleted = conn.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,)).rowcount
    if not deleted:
        raise ApiError(404, "not_found", "Quiz not found")
    ecs_log("info", "Admin deleted a quiz", trace_id=request.state.trace_id, event_action="admin_quiz_deleted",
            student_id=admin_id, **{"quiz.id": quiz_id})
    return Response(status_code=204)


@router.put("/admin/fault-injection", include_in_schema=False)
def set_fault_injection(payload: FaultRequest):
    """Test hook for the resilience demo. Disabled unless ENABLE_FAULT_INJECTION=true."""
    if not ENABLE_FAULT_INJECTION:
        raise ApiError(404, "not_found", "Not found")
    with get_db() as conn:
        require_admin(conn, payload.admin_id)
    FAULT.update(mode=payload.mode, remaining=payload.remaining, delay_seconds=payload.delay_seconds)
    return FAULT


@router.post("/experiments/cardinality", response_model=CardinalityOut, tags=["experiments"],
             summary="Cardinality experiment (A1 Part E.2)", responses=errs(422))
def run_cardinality_experiment(count: int = Query(20, ge=1, le=500)):
    ids = []
    for _ in range(count):
        if CARDINALITY_DEMO_USE_LABEL:
            rid = str(uuid.uuid4())
            DEMO_REQUESTS_TOTAL.labels(request_id=rid).inc()
            ids.append(rid)
        else:
            DEMO_REQUESTS_TOTAL.inc()
    return {"calls_made": count, "using_request_id_label": CARDINALITY_DEMO_USE_LABEL,
            "new_series_created_by_this_call": count if CARDINALITY_DEMO_USE_LABEL else 0,
            "sample_request_ids": ids[:3]}


app.include_router(router)


@app.put("/internal/delay", include_in_schema=False)
async def internal_delay(delay_ms: float = Query(0, ge=0, le=10000)):
    """A3 experiment hook: add a fixed delay to every API call on THIS replica (call it on the replica's
    direct port, not through Envoy -- Envoy answers 404 for /internal)."""
    if not ENABLE_FAULT_INJECTION:
        raise ApiError(404, "not_found", "Not found")
    chaos.set_delay_ms(delay_ms)
    return {"replica": REPLICA_ID, "delay_ms": delay_ms}


# ------------------------------- NON-API -----------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint():
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "service": SERVICE_NAME, "replica": REPLICA_ID}


# ------------------------------- DEMO SEED ---------------------------------
DEMO_STUDENTS = ["alice_demo", "bob_demo", "carol_demo", "dave_demo", "erin_demo"]
DEMO_PASSWORD = "demo123"
UNANSWERED = 9  # marker in the table below: leave the question unanswered
DEMO_ATTEMPTS = [
    ("alice_demo", "python_101", {"0": 1, "1": 1, "2": 0, "3": 2, "4": 1}, 0),
    ("alice_demo", "observability_301", {"0": 1, "1": 1, "2": 1, "3": 1}, 0),
    ("alice_demo", "networks_201", {"0": 2, "1": 1, "2": 2, "3": 9}, 0),
    ("bob_demo", "python_101", {"0": 1, "1": 1, "2": 0, "3": 9, "4": 9}, 0),
    ("bob_demo", "networks_201", {"0": 2, "1": 1, "2": 2, "3": 2}, 1),
    ("bob_demo", "observability_301", {"0": 1, "1": 1, "2": 9, "3": 9}, 0),
    ("carol_demo", "networks_201", {"0": 9, "1": 9, "2": 2, "3": 9}, 0),
    ("carol_demo", "observability_301", {"0": 9, "1": 9, "2": 9, "3": 1}, 0),
    ("dave_demo", "python_101", {"0": 9, "1": 9, "2": 9, "3": 9, "4": 9}, 0),
    ("dave_demo", "networks_201", {"0": 2, "1": 1, "2": 2, "3": 2}, 0),
    ("erin_demo", "observability_301", {"0": 1, "1": 1, "2": 1, "3": 1}, 2),
    ("erin_demo", "python_101", {"0": 1, "1": 1, "2": 0, "3": 2, "4": 9}, 0),
]


def seed_demo_data():
    trace_id = new_trace_id()
    with get_db(write=True) as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (DEMO_STUDENTS[0],)).fetchone():
            return  # DB is persistent now: seed only once
        ids = {}
        for name in DEMO_STUDENTS:
            ids[name] = str(uuid.uuid4())
            conn.execute("INSERT INTO users (id, username, password, is_admin, created_at) VALUES (?,?,?,0,?)",
                         (ids[name], name, DEMO_PASSWORD, now_iso()))
    ecs_log("info", "Seeded demo student accounts", trace_id=trace_id, event_action="demo_seed_users",
            count=len(DEMO_STUDENTS))
    for username, quiz_id, answers, tabs in DEMO_ATTEMPTS:
        uid = ids[username]
        s = exam.start_session(uid, quiz_id, trace_id)
        for _ in range(tabs):
            exam.record_tab_switch(s["session_id"], uid, trace_id)
        answered = {k: v for k, v in answers.items() if v != UNANSWERED}
        exam.submit_attempt(s["session_id"], uid, answered, trace_id=trace_id, allow_scheduled_anomaly=False)
    ecs_log("info", "Demo data seeding complete", trace_id=trace_id, event_action="demo_seed_complete",
            attempts=len(DEMO_ATTEMPTS))
