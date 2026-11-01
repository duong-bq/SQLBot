# Copy of the shared telemetry module (version 0.2.0), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
"""Keep chosen spans out of the SigNoz exporter."""

from collections.abc import Callable, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


def is_llm_span(span: ReadableSpan) -> bool:
    """Spans of an LLM tracing backend that shares the global tracer provider (Langfuse): its own scope, `gen_ai.*`
    and `langfuse.*` attributes. They carry prompts and answers, which must not reach SigNoz."""
    scope = span.instrumentation_scope.name if span.instrumentation_scope else ""
    return scope.startswith("langfuse") or any(
        isinstance(key, str) and key.startswith(("gen_ai", "langfuse.")) for key in (span.attributes or ())
    )


def is_idle_db_span(span: ReadableSpan) -> bool:
    """Database spans nobody asked for: the pool opening a connection (`connect`) and a query with no parent span, which is
    a poll or a probe outside any request or job. Such a span joins no trace and the metrics already count the work.
    Queries inside a request or a job keep their span."""
    attributes = span.attributes or {}
    if "db.system" not in attributes and "db.system.name" not in attributes:
        return False
    return span.name == "connect" or span.parent is None


def is_noise_span(span: ReadableSpan) -> bool:
    """What a service should normally keep out of SigNoz: LLM spans and idle database spans."""
    return is_llm_span(span) or is_idle_db_span(span)


class ExcludeSpans(SpanExporter):
    """Wraps an exporter and drops the spans for which `predicate` is true."""

    def __init__(self, inner: SpanExporter, predicate: Callable[[ReadableSpan], bool]) -> None:
        self._inner = inner
        self._predicate = predicate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        kept = [span for span in spans if not self._predicate(span)]
        return self._inner.export(kept) if kept else SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)
