import asyncio
import io
import json
import logging
import threading
import uuid

import diagnostics
from diagnostics import diagnostic_context
from scrape_runner import ScrapeRunner


def _capture_diagnostics(monkeypatch):
    logger = diagnostics.get_logger()
    old_handlers = list(logger.handlers)
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(diagnostics._JsonFormatter())
    logger.handlers[:] = [capture]
    return logger, stream, old_handlers


def _restore_handlers(old_handlers):
    logger = diagnostics.get_logger()
    logger.handlers[:] = old_handlers


def _json_records(stream):
    """Decodifica i singoli record JSON emessi in questo capture."""
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_json_serialization_allow_list_and_idempotent_configuration(monkeypatch):
    logger, stream, old_streams = _capture_diagnostics(monkeypatch)
    try:
        count = len(logger.handlers)
        assert diagnostics.get_logger() is logger
        assert len(logger.handlers) == count
        correlation_id = uuid.uuid4().hex
        with diagnostic_context(correlation_id=correlation_id, search_id=17, attempt=2,
                                secret="must-not-appear"):
            diagnostics.emit_exception(
                "unexpected_scraper_exception", ValueError("private exception detail"),
                level=logging.INFO,
                operation="scheduled_search", body="private",
                url="https://example.invalid/?token=x")
        assert _json_records(stream) == [{
            "event": "unexpected_scraper_exception", "severity": "INFO", "operation": "scheduled_search",
            "correlation_id": correlation_id, "search_id": 17, "attempt": 2,
            "exception_type": "ValueError",
        }]
        assert "private" not in stream.getvalue()
        assert "example.invalid" not in stream.getvalue()
        assert "private exception detail" not in stream.getvalue()
    finally:
        _restore_handlers(old_streams)


def test_runner_captures_and_isolates_context_per_queued_job():
    runner = ScrapeRunner()
    gate = threading.Event()
    started = threading.Event()

    def inspect_after_gate():
        started.set()
        assert gate.wait(5)
        return diagnostics.current_fields()

    async def scenario():
        with diagnostic_context(correlation_id=uuid.uuid4().hex, search_id=1, attempt=1):
            first_id = diagnostics.current_fields()["correlation_id"]
            first = asyncio.create_task(runner.run(inspect_after_gate))
            await asyncio.to_thread(started.wait, 5)
        with diagnostic_context(correlation_id=uuid.uuid4().hex, search_id=2, attempt=3):
            second_id = diagnostics.current_fields()["correlation_id"]
            second = asyncio.create_task(runner.run(diagnostics.current_fields))
        gate.set()
        first_fields, second_fields = await asyncio.gather(first, second)
        return first_fields, second_fields, first_id, second_id

    try:
        first, second, first_id, second_id = asyncio.run(scenario())
        assert first["correlation_id"] == first_id and first["search_id"] == 1
        assert second["correlation_id"] == second_id and second["search_id"] == 2
        assert second["attempt"] == 3
    finally:
        gate.set()
        runner.shutdown(wait=True)


def test_logger_failure_uses_non_recursive_fixed_stderr_fallback(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(diagnostics, "get_logger", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    monkeypatch.setattr(diagnostics.sys, "stderr", stream)
    diagnostics.emit("ignored", exception_type="PrivateValue")
    assert stream.getvalue() == "flight_bot diagnostic logging failed\n"
    assert "secret" not in stream.getvalue()
    assert "PrivateValue" not in stream.getvalue()


def test_handler_error_fallback_does_not_render_record(monkeypatch):
    logger = diagnostics.get_logger()
    handler = logger.handlers[0]
    stream = io.StringIO()
    monkeypatch.setattr(handler, "stream", stream)
    record = logging.LogRecord("x", logging.ERROR, "x.py", 1, "sensitive", (), None)
    handler.handleError(record)
    assert stream.getvalue() == "flight_bot diagnostic logging failed\n"


def test_invalid_diagnostic_values_are_omitted(monkeypatch):
    logger, stream, old_streams = _capture_diagnostics(monkeypatch)
    try:
        with diagnostic_context(correlation_id="not-an-id", leg="sideways", attempt=True):
            diagnostics.emit("unknown_event", operation="arbitrary", stage="private",
                             search_id=True, status_code=700, response_bytes=-1,
                             body_sha256="secret", has_expected_marker=1,
                             consent_redirect="yes", exception_type="ValueError: secret")
        assert _json_records(stream) == [{"severity": "INFO"}]
    finally:
        _restore_handlers(old_streams)


def test_context_is_restored_when_monitored_function_raises():
    marker = uuid.uuid4().hex
    try:
        with diagnostic_context(correlation_id=marker):
            raise LookupError("original")
    except LookupError:
        pass
    assert diagnostics.current_fields() == {}


def test_response_diagnostic_hash_failure_does_not_fail_scrape(monkeypatch):
    from scrapers import google_flights

    monkeypatch.setattr(google_flights.hashlib, "sha256",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("diagnostic failure")))
    class Response:
        text = "valid body"
    assert google_flights._response_text(Response()) == "valid body"


def test_urlsplit_diagnostic_failure_does_not_fail_request(monkeypatch):
    from scrapers import google_flights

    class Response:
        status_code = 200
        url = "https://example.invalid"
        def raise_for_status(self):
            pass
    class Client:
        def get(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(google_flights, "_client", Client())
    monkeypatch.setattr(google_flights, "urlsplit",
                        lambda *_: (_ for _ in ()).throw(ValueError("bad URL")))
    assert google_flights._request("get", "https://example.invalid") is not None
