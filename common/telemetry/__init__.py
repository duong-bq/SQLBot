# Copy of the shared telemetry module (version 0.2.0), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
from .config import service_name
from .logs import (
    ContractFormatter,
    HandledAccessLog,
    RequestContextFilter,
    RequestContextMiddleware,
    configure_logging,
)
from .requestline import RequestLineMiddleware, describe_request, log_request, note_error
from .setup import (
    configure_telemetry,
    instrument_app,
    instrument_engine,
    tracing_enabled,
)
from .spans import ExcludeSpans, is_idle_db_span, is_llm_span, is_noise_span

__all__ = [
    "ContractFormatter",
    "ExcludeSpans",
    "HandledAccessLog",
    "RequestContextFilter",
    "RequestContextMiddleware",
    "RequestLineMiddleware",
    "configure_logging",
    "configure_telemetry",
    "describe_request",
    "instrument_app",
    "instrument_engine",
    "is_idle_db_span",
    "is_llm_span",
    "is_noise_span",
    "log_request",
    "note_error",
    "service_name",
    "tracing_enabled",
]
