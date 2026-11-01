# Copy of the shared telemetry module (version 0.2.0), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
"""One JSON object per line on stdout, in the shape the node log agent parses (otel-integration log contract).

- `ContractFormatter`: timestamp, level, message, service.name, logger, the keys passed with `extra=`,
  trace ids of the active span and the exception in `exception.*`.
- `RequestContextMiddleware` and `RequestContextFilter`: every line written while a request is handled names
  the call (`http.request.method`, `url.path`), so the business line replaces the access line.
- `HandledAccessLog`: drops the uvicorn access lines those business lines already cover.
"""

import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.utils import suppress_instrumentation

from .config import service_name

_LEVELS = {"WARNING": "WARN", "CRITICAL": "FATAL"}

# Attributes the stdlib puts on every LogRecord; anything else came from `extra=`.
_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}

# Extra attributes that are never written to the line. uvicorn adds `color_message`, a copy of its message with terminal
# colour codes, for its own console formatter; the plain message is already the body.
SKIPPED_EXTRAS = frozenset({"color_message"})

PROBE_PATHS = ("/healthz", "/readyz")


def log_timezone() -> timezone:
    # A fixed offset: no daylight saving, and no time zone data needed in the image. Default is Vietnam time.
    return timezone(timedelta(hours=float(os.environ.get("LOG_UTC_OFFSET_HOURS", "7"))))


class ContractFormatter(logging.Formatter):
    def __init__(self, name: str, redact: Callable[[str], str] | None = None) -> None:
        super().__init__()
        self._service_name = name
        self._redact = redact or (lambda text: text)

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, log_timezone()).isoformat(
                timespec="milliseconds"
            ),
            "level": _LEVELS.get(record.levelname, record.levelname),
            "message": record.getMessage(),
            "service.name": self._service_name,
            "logger": record.name,
        }
        for key, value in vars(record).items():
            if key not in _RECORD_FIELDS and key not in SKIPPED_EXTRAS and not key.startswith("_"):
                out[key] = value
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            out["trace_id"] = format(context.trace_id, "032x")
            out["span_id"] = format(context.span_id, "016x")
        if record.exc_info and record.exc_info[1] is not None:
            out["exception.type"] = record.exc_info[0].__name__
            out["exception.message"] = self._redact(str(record.exc_info[1]))
        # A traceback is already redacted by the filter that fills `exc_text`; one line, escaped by JSON.
        stacktrace = record.exc_text or (
            self.formatException(record.exc_info) if record.exc_info else None
        )
        if stacktrace:
            out["exception.stacktrace"] = self._redact(stacktrace)
        return json.dumps(out, ensure_ascii=False, default=str)


_request: ContextVar[tuple[str, str] | None] = ContextVar("telemetry_request", default=None)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = _request.get()
        if context is not None and not hasattr(record, "http.request.method"):
            setattr(record, "http.request.method", context[0])
            setattr(record, "url.path", context[1])
        return True


class RequestContextMiddleware:
    # Plain ASGI, not BaseHTTPMiddleware: that one breaks streaming responses and the context of sync routes
    # that run in a thread. A task started during the request copies the context, so its lines name the call.
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = _request.set((scope["method"], scope["path"]))
        try:
            if scope["path"] in PROBE_PATHS:
                # The probe has no server span (instrument_app excludes it), so the dependency checks it runs would be
                # root spans nobody can join to anything: every 10 s per pod. Instrumentations skip their span here.
                with suppress_instrumentation():
                    await self._app(scope, receive, send)
            else:
                await self._app(scope, receive, send)
        finally:
            _request.reset(token)


class HandledAccessLog(logging.Filter):
    """Every call already logs what it did (done, read, rejected), so the uvicorn access line only repeats it:
    drop it below 500. A 5xx keeps its line. A probe is always dropped, because a failing `/readyz` logs which
    dependency is down."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[4], int):
            if str(args[2]).split("?")[0] in PROBE_PATHS:
                return False
            return args[4] >= 500
        return True


class _StdStreamHandler(logging.StreamHandler):
    """Writes to whatever `sys.stdout` or `sys.stderr` is at the time of each record, as logging's own stderr handler does.

    A handler that keeps the stream it saw at setup writes to a closed file once a test runner or a launcher has
    replaced the stream and closed the old one, and logging then prints a traceback at exit.
    """

    def __init__(self, stream_name: str) -> None:
        logging.Handler.__init__(self)
        self._stream_name = stream_name

    @property
    def stream(self):  # type: ignore[override]
        return getattr(sys, self._stream_name)


_handler: logging.Handler | None = None


def configure_logging(
    default_service_name: str,
    *,
    level: str | None = None,
    logger_names: Iterable[str] = (),
    extra_filters: Iterable[logging.Filter] = (),
    redact: Callable[[str], str] | None = None,
    replace_root_handlers: bool = False,
    quiet_loggers: Iterable[str] = (),
    to_stderr: bool = False,
    force: bool = False,
) -> logging.Handler:
    """Install the contract handler on stdout and return it.

    `level` (default env `LOG_LEVEL`, then INFO) applies to `logger_names`; with none given it applies to the
    root logger. Naming only the application's own loggers keeps client libraries (HTTP, Kubernetes, workflow
    engines) at WARNING. `extra_filters` run after the request context, so a redaction filter sees the path.
    `to_stderr` is for a process whose stdout carries data (a command-line tool that prints its result): logs go to
    stderr and the container log agent reads both streams.
    """
    global _handler
    if _handler is not None and not force:
        return _handler
    handler = _StdStreamHandler("stderr" if to_stderr else "stdout")
    handler.setFormatter(ContractFormatter(service_name(default_service_name), redact))
    handler.addFilter(RequestContextFilter())
    for extra in extra_filters:
        handler.addFilter(extra)
    names = tuple(logger_names)
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    if replace_root_handlers:
        root.handlers = [handler]
    else:
        root.addHandler(handler)
    for name in names or (None,):
        logging.getLogger(name).setLevel(level_name)
    for name in quiet_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn installs its own non-propagating handlers unless it runs with log_config=None; its lines would
    # stay in the plain format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HandledAccessLog) for f in access_log.filters):
        access_log.addFilter(HandledAccessLog())
    _handler = handler
    return handler
