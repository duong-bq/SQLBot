# Copy of the shared telemetry module (version 0.2.1), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
"""One log line per API request, worded as a sentence about what the request did.

`describe_request` turns the route template into the sentence (`Created the api key`), `log_request` writes the line with the
path parameters (`key_id`) in the text and as fields, and `RequestLineMiddleware` does both for a service that has nothing
more to add. A service with its own request pipeline (a database sink, a body log) calls `log_request` from its sink.

A request has one line, and its level follows the status: INFO below 400, WARN for 4xx, ERROR from 500. An error handler does
not log the rejection itself: it calls `note_error`, and the line carries `error_code`, `error_message` and `error_details`.
Field names are the OpenTelemetry ones (`http.request.method`, `url.path`, `http.response.status_code`).
"""

import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from .logs import _RECORD_FIELDS, PROBE_PATHS

# The last route segment is an action rather than a resource: (base form, past tense).
_ACTIONS = {
    "verify": ("verify", "Verified"),
    "rotate": ("rotate", "Rotated"),
    "refresh": ("refresh", "Refreshed"),
    "query": ("query", "Queried"),
    "ask": ("ask", "Asked"),
    "search": ("search", "Searched"),
    "sync": ("sync", "Synced"),
    "generate": ("generate", "Generated"),
    "callback-test": ("test the callback of", "Tested the callback of"),
    "wake": ("wake", "Woke"),
    "result": ("record the result of", "Recorded the result of"),
    "reindex": ("reindex", "Reindexed"),
    "excel": ("import", "Imported"),
}

_BY_METHOD = {
    "POST": ("create", "Created"),
    "PUT": ("update", "Updated"),
    "PATCH": ("update", "Updated"),
    "DELETE": ("delete", "Deleted"),
}

_UNCOUNTABLE = {"knowledge", "status", "access", "usage", "healthz", "readyz", "openapi.json"}

# A route-parameter name must not shadow an attribute of the log record, or the record cannot be built.
_RESERVED = _RECORD_FIELDS | {"message", "asctime"}


def _singular(word: str) -> str:
    if word in _UNCOUNTABLE:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ss", "us")):
        return word
    return word.removesuffix("s")


def _is_param(segment: str) -> bool:
    return segment.startswith("{") or re.fullmatch(r"[0-9a-f-]{16,}", segment) is not None


def describe_request(method: str, route: str, status_code: int, error_code: str | None = None) -> str:
    """For example `Created the api key`, `Fetched the api key`, `Failed to verify the credential (credential_invalid)`."""
    segments = [s for s in route.split("/") if s and s not in ("v1", "api", "internal")]
    literals = [s for s in segments if not _is_param(s)]
    if not literals:
        return f"Handled {method} {route}"
    ends_with_param = _is_param(segments[-1])
    last = literals[-1]
    if last in _ACTIONS and len(literals) > 1:
        base, past = _ACTIONS[last]
        noun = _singular(literals[-2])
    elif last in _ACTIONS:
        base, past = _ACTIONS[last]
        noun = "request"
    elif method == "GET":
        listing = not ends_with_param and last not in _UNCOUNTABLE and last != "extraction-status"
        base, past = ("list", "Listed") if listing else ("fetch", "Fetched")
        noun = last if listing else _singular(last)
    else:
        base, past = _BY_METHOD.get(method, ("handle", "Handled"))
        noun = _singular(last) if ends_with_param or method == "POST" else last
    noun = noun.replace("-", " ").replace("_", " ")
    if status_code < 400:
        return f"{past} the {noun}"
    reason = f" ({error_code})" if error_code else ""
    return f"Failed to {base} the {noun}{reason}"


def note_error(request: Any, code: str, message: str | None = None, details: Any = None) -> None:
    """Called by an error handler with the `Request`: the request line reads the values from `request.state`."""
    request.state.error_code = code
    request.state.error_message = message
    request.state.error_details = details


def log_request(
    logger: logging.Logger,
    *,
    method: str,
    route: str | None,
    path: str,
    status_code: int,
    duration_ms: int,
    path_params: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_details: Any = None,
    **fields: Any,
) -> None:
    """The sentence, then `(HTTP status, <param> <value>, <ms> ms)`; the same values are fields, so the line can be filtered."""
    params = {name: str(value) for name, value in (path_params or {}).items()}
    target = "".join(f", {name} {value}" for name, value in params.items())
    sentence = describe_request(method, route or path, status_code, error_code)
    level = logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO
    logger.log(
        level,
        f"{sentence} (HTTP {status_code}{target}, {duration_ms} ms)",
        extra={
            **{name: value for name, value in params.items() if name not in _RESERVED},
            **{name: value for name, value in fields.items() if name not in _RESERVED},
            "route": route,
            "http.request.method": method,
            "url.path": path,
            "http.response.status_code": status_code,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "error_message": error_message,
            "error_details": error_details,
        },
    )


class RequestLineMiddleware:
    """Plain ASGI middleware (no `BaseHTTPMiddleware`, which breaks streaming responses). Probes get no line.

    `extra_fields(scope)` adds service fields to the line, for example the caller held in `scope["state"]`.
    """

    def __init__(
        self,
        app: Any,
        *,
        logger_name: str = "app.request",
        extra_fields: Callable[[Any], Mapping[str, Any]] | None = None,
    ) -> None:
        self._app = app
        self._logger = logging.getLogger(logger_name)
        self._extra_fields = extra_fields

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] in PROBE_PATHS:
            await self._app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def capture(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, capture)
        finally:
            log_request(
                self._logger,
                method=scope["method"],
                route=getattr(scope.get("route"), "path", None),
                path=scope["path"],
                status_code=status,
                duration_ms=round((time.perf_counter() - started) * 1000),
                path_params=scope.get("path_params"),
                error_code=(scope.get("state") or {}).get("error_code"),
                error_message=(scope.get("state") or {}).get("error_message"),
                error_details=(scope.get("state") or {}).get("error_details"),
                **(self._extra_fields(scope) if self._extra_fields else {}),
            )
