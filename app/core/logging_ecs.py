"""ECS-structured JSON logging shared by the HTTP app and the gRPC service."""
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone

SERVICE_NAME = os.environ.get("SERVICE_NAME", "veritas-app")
_TRACE_RE = re.compile(r"^[0-9a-f]{32}$")


class ECSJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
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
        extra_fields = getattr(record, "ecs_extra", None)
        if extra_fields:
            payload.update(extra_fields)
        payload = {k: v for k, v in payload.items() if v is not None}
        return json.dumps(payload, separators=(",", ":"))


_logger = logging.getLogger("veritas")
_logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(stream=sys.stdout)
_handler.setFormatter(ECSJsonFormatter())
_logger.handlers = [_handler]
_logger.propagate = False


def new_trace_id() -> str:
    """32 hex chars = W3C trace-context trace-id length."""
    return uuid.uuid4().hex


def is_valid_trace_id(value: str) -> bool:
    return bool(value) and bool(_TRACE_RE.match(value))


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
