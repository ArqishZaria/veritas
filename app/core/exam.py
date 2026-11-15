"""Business rules for taking an exam. Used by BOTH the HTTP handlers and the
gRPC handler -- this module is the single implementation of the rules."""
import json
import threading
import time
import uuid

from app.core import metrics
from app.core.db import get_db, now_iso
from app.core.logging_ecs import ecs_log


class DomainError(Exception):
    """code: NOT_FOUND | PERMISSION_DENIED | INVALID_ARGUMENT | CONFLICT"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_lock = threading.Lock()
_submission_counter = 0


def _scheduled_anomaly() -> bool:
    global _submission_counter
    with _lock:
        _submission_counter += 1
        return _submission_counter % 5 == 0


def start_session(user_id: str, quiz_id: str, trace_id: str = None) -> dict:
    session_id, started = str(uuid.uuid4()), now_iso()
    with get_db(write=True) as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise DomainError("NOT_FOUND", "User not found")
        if not conn.execute("SELECT 1 FROM quizzes WHERE id = ?", (quiz_id,)).fetchone():
            raise DomainError("NOT_FOUND", "Quiz not found")
        conn.execute("INSERT INTO exam_sessions (id, user_id, quiz_id, started_at) VALUES (?,?,?,?)",
                     (session_id, user_id, quiz_id, started))
    ecs_log("info", "Student started exam", trace_id=trace_id, event_action="exam_started",
            student_id=user_id, **{"quiz.id": quiz_id, "session.id": session_id})
    return {"session_id": session_id, "quiz_id": quiz_id, "started_at": started}


def record_tab_switch(session_id: str, user_id: str, trace_id: str = None) -> int:
    with get_db(write=True) as conn:
        s = conn.execute("SELECT * FROM exam_sessions WHERE id = ?", (session_id,)).fetchone()
        if not s:
            raise DomainError("NOT_FOUND", "Session not found")
        if s["user_id"] != user_id:
            raise DomainError("PERMISSION_DENIED", "Session belongs to another user")
        if s["ended_at"]:
            raise DomainError("CONFLICT", "Session already submitted")
        conn.execute("UPDATE exam_sessions SET tab_switch_count = tab_switch_count + 1 WHERE id = ?", (session_id,))
        count = s["tab_switch_count"] + 1
    ecs_log("warn", "Tab switch detected during exam", trace_id=trace_id, event_action="tab_switched",
            student_id=user_id, **{"quiz.id": s["quiz_id"], "session.id": session_id, "tab_switch_count": count})
    return count


def _result(row) -> dict:
    return {"attempt_id": row["id"], "score": row["score"], "status": row["status"],
            "cheated": row["status"] == "cheated", "correct": row["correct"], "total": row["total"]}


def submit_attempt(session_id: str, user_id: str, answers: dict, client_tab_switches: int = 0,
                   simulate_anomaly: bool = False, trace_id: str = None,
                   allow_scheduled_anomaly: bool = True):
    """Grade + persist. Returns (result_dict, replayed).

    Idempotent: the server-issued session_id is the natural key. The first call
    creates the attempt; later calls with the same answers return the stored
    attempt (replayed=True); different answers -> CONFLICT."""
    grading_start = time.perf_counter()
    scheduled = _scheduled_anomaly() if allow_scheduled_anomaly else False
    if simulate_anomaly or scheduled:
        time.sleep(0.5)  # outside the DB transaction so we never hold the write lock
        ecs_log("warn", "Injected artificial grading delay (anomaly experiment)", trace_id=trace_id,
                event_action="anomaly_injected", student_id=user_id,
                **{"session.id": session_id, "delay_ms": 500})
    answers = {int(k): int(v) for k, v in (answers or {}).items()}

    with get_db(write=True) as conn:
        s = conn.execute("SELECT * FROM exam_sessions WHERE id = ?", (session_id,)).fetchone()
        if not s:
            raise DomainError("NOT_FOUND", "Session not found")
        if s["user_id"] != user_id:
            raise DomainError("PERMISSION_DENIED", "Session belongs to another user")
        quiz_id = s["quiz_id"]

        if s["attempt_id"]:
            att = conn.execute("SELECT * FROM attempts WHERE id = ?", (s["attempt_id"],)).fetchone()
            stored = {int(k): int(v) for k, v in json.loads(att["answers_json"]).items()}
            if stored != answers:
                raise DomainError("CONFLICT", "Session already submitted with different answers")
            result, replayed = _result(att), True
        else:
            q = conn.execute("SELECT questions_json FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
            if not q:
                raise DomainError("NOT_FOUND", "Quiz no longer exists")
            questions = json.loads(q["questions_json"])
            for idx, opt in answers.items():
                if not (0 <= idx < len(questions)):
                    raise DomainError("INVALID_ARGUMENT", f"Question index {idx} out of range")
                if not (0 <= opt < len(questions[idx]["options"])):
                    raise DomainError("INVALID_ARGUMENT", f"Option {opt} out of range for question {idx}")
            correct = sum(1 for i, qq in enumerate(questions) if answers.get(i) == qq["answer"])
            raw = round(correct / len(questions) * 100, 2) if questions else 0.0
            tabs = max(s["tab_switch_count"], int(client_tab_switches or 0))
            if tabs > 0:
                final, status = 0.0, "cheated"
            else:
                final, status = raw, ("passed" if raw >= 50 else "failed")
            attempt_id, ts = str(uuid.uuid4()), now_iso()
            conn.execute(
                """INSERT INTO attempts (id, session_id, user_id, quiz_id, score, status, tab_switch_count,
                   correct, total, answers_json, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, session_id, user_id, quiz_id, final, status, tabs, correct, len(questions),
                 json.dumps({str(k): v for k, v in answers.items()}), ts))
            conn.execute("UPDATE exam_sessions SET ended_at = ?, attempt_id = ? WHERE id = ?",
                         (ts, attempt_id, session_id))
            result = {"attempt_id": attempt_id, "score": final, "status": status,
                      "cheated": status == "cheated", "correct": correct, "total": len(questions)}
            replayed = False

    duration = time.perf_counter() - grading_start
    if replayed:
        metrics.IDEMPOTENT_REPLAYS_TOTAL.labels(operation="submit_attempt").inc()
        ecs_log("info", "Duplicate submission replayed with stored result", trace_id=trace_id,
                event_action="submit_replayed", student_id=user_id,
                **{"quiz.id": quiz_id, "session.id": session_id})
        return result, True

    metrics.GRADING_DURATION_SECONDS.labels(quiz_id=quiz_id).observe(duration)
    metrics.QUIZ_SUBMISSIONS_TOTAL.labels(status=result["status"], quiz_id=quiz_id).inc()
    cheated = result["cheated"]
    ecs_log("warn" if cheated else "info",
            "Cheating detected: tab-switch penalty applied" if cheated else "Quiz submitted and auto-graded",
            trace_id=trace_id, event_action="quiz_submitted",
            event_duration_ns=int(duration * 1_000_000_000), student_id=user_id,
            **{"quiz.id": quiz_id, "session.id": session_id, "score": result["score"],
               "status": result["status"]})
    return result, False
