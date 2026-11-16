"""A3 experiment hook: a fixed per-call delay on one replica (set at runtime via PUT /internal/delay,
or at start-up via INJECT_DELAY_MS). Used by HTTP middleware and the gRPC interceptor. Per-process state."""
import os

_delay_ms = float(os.environ.get("INJECT_DELAY_MS", "0"))


def get_delay_s() -> float:
    return _delay_ms / 1000.0


def set_delay_ms(value: float) -> None:
    global _delay_ms
    _delay_ms = float(value)
