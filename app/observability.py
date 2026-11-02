"""OpenTelemetry tracing + Prometheus metrics setup.

Traces and logs are exported OTLP -> otel-collector, which forwards logs to Loki. Metrics are
served on their own internal port and scraped by Prometheus. Logs must never contain message
content: private chats are not to be readable there.
"""
from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, start_http_server
from pythonjsonlogger import jsonlogger

from app.config import get_settings

_settings = get_settings()

# --- Prometheus metrics ---
llm_requests_total = Counter("llm_requests_total", "LLM calls", ["model", "status"])
llm_tokens_total = Counter("llm_tokens_total", "LLM tokens", ["model", "kind"])  # kind=prompt|completion
llm_latency_seconds = Histogram("llm_latency_seconds", "LLM call latency", ["model"])
acl_decisions_total = Counter("acl_decisions_total", "ACL decisions", ["decision", "action"])
graph_node_total = Counter("graph_node_total", "LangGraph node executions", ["node"])

# --- Request Manager metrics (labeled by department) ---
rm_requests_total = Counter("rm_requests_total", "Request Manager requests", ["department", "outcome"])
rm_request_latency_seconds = Histogram("rm_request_latency_seconds", "RM end-to-end latency", ["department"])
rm_quota_rejections_total = Counter("rm_quota_rejections_total", "RM quota rejections", ["department"])
rm_approvals_total = Counter("rm_approvals_total", "RM approvals by state", ["department", "state"])
rm_critical_gated_total = Counter("rm_critical_gated_total", "RM critical requests gated", ["department"])

tracer = trace.get_tracer(_settings.otel_service_name)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def _service_resource() -> Resource:
    return Resource.create({SERVICE_NAME: _settings.otel_service_name})


def log_export_handler(exporter: LogRecordExporter, processor_cls=BatchLogRecordProcessor) -> LoggingHandler:
    provider = LoggerProvider(resource=_service_resource())
    provider.add_log_record_processor(processor_cls(exporter))
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    # The exporter logs its own failures. Exporting those would feed back into it.
    handler.addFilter(lambda record: not record.name.startswith("opentelemetry"))
    return handler


def setup_log_export() -> None:
    exporter = OTLPLogExporter(endpoint=_settings.otel_exporter_otlp_endpoint, insecure=True)
    logging.getLogger().addHandler(log_export_handler(exporter))


def start_metrics_server(port: int | None = None):
    """Serve /metrics on a port of its own. docker-compose never publishes it, so Prometheus
    can reach it over the internal network and the internet can't."""
    server, _thread = start_http_server(_settings.metrics_port if port is None else port)
    return server


def setup_tracing() -> None:
    provider = TracerProvider(resource=_service_resource())
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=_settings.otel_exporter_otlp_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
