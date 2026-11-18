"""Logging diagnostico strutturato, limitato a campi esplicitamente consentiti."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from contextlib import contextmanager


_FIELDS = (
    "event", "severity", "operation", "stage", "correlation_id", "cycle_id",
    "search_id", "attempt", "leg", "exception_type", "status_code",
    "response_bytes", "body_sha256", "has_expected_marker", "consent_redirect",
)
_context = contextvars.ContextVar("flight_bot_diagnostic_context", default={})
_LOGGER_NAME = "flight_bot.diagnostic"
_FALLBACK = "flight_bot diagnostic logging failed\n"
_VALUES = {
    "event": {"http_transport_error", "http_status_error", "http_response", "response_read_error",
              "response_read", "response_anomaly", "consent_detected", "consent_anomaly",
              "consent_submitted", "consent_redirect", "parse_anomaly", "parse_started",
              "parse_completed", "unexpected_scraper_exception", "unexpected_manual_search_exception",
              "unexpected_scheduler_exception"},
    "operation": {"manual_search", "scheduled_search"},
    "stage": {"http_get", "http_post", "response_read", "consent_parse", "consent_submit",
              "consent_detect", "parse", "parse_or_fetch", "scrape", "store_or_alert"},
    "leg": {"outbound", "return"},
}
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _valid(key, value):
    if key in _VALUES:
        return isinstance(value, str) and value in _VALUES[key]
    if key in ("correlation_id", "cycle_id"):
        return isinstance(value, str) and _HEX_ID.fullmatch(value) is not None
    if key == "search_id":
        return type(value) is int and value > 0
    if key == "attempt":
        return type(value) is int and value > 0
    if key == "status_code":
        return type(value) is int and 100 <= value <= 599
    if key == "response_bytes":
        return type(value) is int and value >= 0
    if key == "body_sha256":
        return isinstance(value, str) and _SHA256.fullmatch(value) is not None
    if key in ("has_expected_marker", "consent_redirect"):
        return type(value) is bool
    if key == "exception_type":
        return isinstance(value, str) and _CLASS_NAME.fullmatch(value) is not None
    return key == "severity" and isinstance(value, str)


def new_id() -> str:
    return uuid.uuid4().hex


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        source = getattr(record, "diagnostic_fields", {})
        data = {key: source[key] for key in _FIELDS if key in source}
        data["severity"] = record.levelname
        return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


class _SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        try:
            self.stream.write(_FALLBACK)
            self.stream.flush()
        except Exception:
            pass


def get_logger() -> logging.Logger:
    """Restituisce il logger JSON dedicato, configurato una sola volta."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not getattr(logger, "_flight_bot_configured", False):
        handler = _SafeStreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger._flight_bot_configured = True
    return logger


@contextmanager
def diagnostic_context(**fields):
    current = dict(_context.get())
    current.update({key: value for key, value in fields.items()
                    if key in _FIELDS and _valid(key, value)})
    token = _context.set(current)
    try:
        yield current
    finally:
        _context.reset(token)


def context_snapshot() -> contextvars.Context:
    """Cattura il contesto corrente prima di accodare lavoro nel thread runner."""
    return contextvars.copy_context()


def current_fields() -> dict:
    return dict(_context.get())


def emit(event: str, level: int = logging.INFO, **fields) -> None:
    try:
        allowed = {key: value for key, value in _context.get().items()
                   if key in _FIELDS and _valid(key, value)}
        allowed.update({key: value for key, value in fields.items()
                        if key in _FIELDS and _valid(key, value)})
        if _valid("event", event):
            allowed["event"] = event
        get_logger().log(level, "diagnostic", extra={"diagnostic_fields": allowed})
    except Exception:
        try:
            sys.stderr.write(_FALLBACK)
            sys.stderr.flush()
        except Exception:
            pass


def emit_exception(event: str, exc: BaseException, level: int = logging.ERROR, **fields) -> None:
    emit(event, level, exception_type=type(exc).__name__, **fields)
