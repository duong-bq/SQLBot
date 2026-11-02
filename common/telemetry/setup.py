# Copy of the shared telemetry module (version 0.2.1), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
"""Metrics and traces over OTLP straight to the collector; logs stay on stdout for the node agent.

Everything is read from the standard OTEL_* variables, which the elcom-service chart sets when `otel.enabled`
(endpoint, exporters, protocol, interval, service name, resource attributes). Without the metrics endpoint nothing
is set up and every instrument is a no-op, so tests and local runs need nothing.
"""

import os
from collections.abc import Callable
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .spans import ExcludeSpans

_tracing = False


def configure_telemetry(
    default_service_name: str,
    *,
    exclude_spans: Callable[[ReadableSpan], bool] | None = None,
    urllib3: bool = False,
    httpx: bool = False,
) -> bool:
    """Set up the meter and tracer providers. Returns False (and sets up nothing) without an endpoint.

    `exclude_spans` keeps the spans it matches out of the SigNoz exporter (normally `is_noise_span`: LLM spans and idle database spans).
    `urllib3` and `httpx` instrument the outgoing HTTP of those clients (needs the matching extra).
    """
    global _tracing
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    if not endpoint:
        return False
    # The service name and the resource attributes come from OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES.
    os.environ.setdefault("OTEL_SERVICE_NAME", default_service_name)
    resource = Resource.create()
    if os.environ.get("OTEL_METRICS_EXPORTER", "otlp") == "otlp":
        metrics.set_meter_provider(
            MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
            )
        )
    # Without a span processor the provider exports nothing but still gives requests a trace id for the log lines.
    provider = TracerProvider(resource=resource)
    if os.environ.get("OTEL_TRACES_EXPORTER", "otlp") == "otlp":
        exporter: Any = OTLPSpanExporter()
        if exclude_spans is not None:
            exporter = ExcludeSpans(exporter, exclude_spans)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracing = True
    if urllib3:
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

        URLLib3Instrumentor().instrument()
    if httpx:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    return True


def tracing_enabled() -> bool:
    return _tracing


def instrument_app(app: Any) -> None:
    """Request spans and the http.server.* metrics of a FastAPI app. Probes are excluded: they would dominate the counts."""
    if _tracing:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")


def instrument_engine(engine: Any) -> None:
    """One span per SQL statement under the request or job that issued it. Takes a sync or an async engine."""
    if _tracing:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=getattr(engine, "sync_engine", engine))
