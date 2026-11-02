# Copy of the shared telemetry module (version 0.2.1), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
"""Temporal: link the API request, the workflow and its activities into one trace."""

from typing import Any

from .setup import tracing_enabled


def tracing_interceptors() -> list[Any]:
    """Interceptors for `Client.connect(..., interceptors=...)`. The worker takes the interceptors of its client."""
    if not tracing_enabled():
        return []
    from temporalio.contrib.opentelemetry import TracingInterceptor

    return [TracingInterceptor()]
