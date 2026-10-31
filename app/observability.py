"""OpenTelemetry tracing + Prometheus metrics setup.

Traces are exported OTLP -> otel-collector. Metrics are exposed on /metrics and
scraped by Prometheus. Structured JSON logs are shipped to Loki by the collector.
"""
from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram
from pythonjsonlogger import jsonlogger

from app.config import get_settings

_settings = get_settings()

# --- Prometheus metrics ---
llm_requests_total = Counter("llm_requests_total", "LLM calls", ["model", "status"])
llm_tokens_total = Counter("llm_tokens_total", "LLM tokens", ["model", "kind"])  # kind=prompt|completion
llm_latency_seconds = Histogram("llm_latency_seconds", "LLM call latency", ["model"])
acl_decisions_total = Counter("acl_decisions_total", "ACL decisions", ["decision", "action"])
graph_node_total = Counter("graph_node_total", "LangGraph node executions", ["node"])

tracer = trace.get_tracer(_settings.otel_service_name)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def setup_tracing() -> None:
    resource = Resource.create({SERVICE_NAME: _settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=_settings.otel_exporter_otlp_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
