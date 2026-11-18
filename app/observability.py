"""OpenTelemetry tracing + Prometheus metrics setup.

Traces and logs are exported OTLP -> otel-collector, which forwards logs to Loki. Metrics are
served on their own internal port and scraped by Prometheus. Logs must never contain message
content: private chats are not to be readable there.
"""
from __future__ import annotations

import logging
import re

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from pythonjsonlogger import jsonlogger

from app.config import get_settings

_settings = get_settings()

# --- Prometheus metrics ---
llm_requests_total = Counter("llm_requests_total", "LLM calls", ["model", "status"])
llm_tokens_total = Counter("llm_tokens_total", "LLM tokens", ["model", "kind"])  # kind=prompt|completion
# The same by task: the app function that asked (e.g. scout.judge, guards.triage_verdict), app/llm/client.py.
llm_tier_calls_total = Counter("llm_tier_calls_total", "Claude calls by model-manager tier", ["tier", "model"])
llm_cost_usd_total = Counter("llm_cost_usd_total", "Claude spend in USD (list prices)", ["tier", "model"])
def _month(field: str):
    def read() -> float:
        from app.config import get_settings

        if get_settings().llm_provider != "anthropic":
            return 0.0
        from app.llm import model_manager

        return float(model_manager.month_summary()[field])
    return read


# This month's Claude spend from the shared ledger (every process), for Grafana and alert rules.
for _name, _field, _doc in (("llm_month_spend_usd", "spent_usd", "Claude spend this month (USD)"),
                            ("llm_month_projected_usd", "projected_usd", "Claude spend projected for the month (USD)"),
                            ("llm_month_budget_usd", "budget_usd", "Monthly Claude budget (USD)")):
    Gauge(_name, _doc).set_function(_month(_field))
llm_task_tokens_total = Counter("llm_task_tokens_total", "LLM tokens by task", ["task", "kind"])
llm_latency_seconds = Histogram("llm_latency_seconds", "LLM call latency", ["model"])
acl_decisions_total = Counter("acl_decisions_total", "ACL decisions", ["decision", "action"])
graph_node_total = Counter("graph_node_total", "LangGraph node executions", ["node"])

# --- Request Manager metrics (labeled by department) ---
rm_requests_total = Counter("rm_requests_total", "Request Manager requests", ["department", "outcome"])
rm_request_latency_seconds = Histogram("rm_request_latency_seconds", "RM end-to-end latency", ["department"])
rm_quota_rejections_total = Counter("rm_quota_rejections_total", "RM quota rejections", ["department"])
rm_approvals_total = Counter("rm_approvals_total", "RM approvals by state", ["department", "state"])
rm_critical_gated_total = Counter("rm_critical_gated_total", "RM critical requests gated", ["department"])

# Free disk space, read at scrape time (the root disk here is small: weights, logs, MinIO).
host_disk_free_ratio = Gauge("host_disk_free_ratio", "Fraction of the disk that is free", ["mount"])


def _free_ratio(path: str) -> float:
    import shutil

    usage = shutil.disk_usage(path)
    return usage.free / usage.total if usage.total else 0.0


host_disk_free_ratio.labels(mount="/").set_function(lambda: _free_ratio("/"))

tracer = trace.get_tracer(_settings.otel_service_name)


# Query-string values that must never reach a log: Meta's webhook verify token (sent as
# hub.verify_token or hub_verify_token), OAuth codes/states and anything named like a token or key.
_SECRET_QUERY = re.compile(
    r"([?&](?:hub[._]verify_token|[\w.]*(?:token|secret|password|api_key|apikey)|code|state)=)[^&\s]*",
    re.IGNORECASE,
)


def redact_query(text: str) -> str:
    return _SECRET_QUERY.sub(r"\1***", text)


class RedactQueryFilter(logging.Filter):
    """Masks secret query values in uvicorn's access log line ("%s - %s %s HTTP/%s %d", path is args[2])."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_query(args[2]), *args[3:])
        elif isinstance(record.msg, str) and "?" in record.msg:
            record.msg = redact_query(record.msg)
        return True


def setup_logging() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQueryFilter) for f in access.filters):
        access.addFilter(RedactQueryFilter())
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


def setup_tracing(export: bool | None = None) -> TracerProvider:
    provider = TracerProvider(resource=_service_resource())
    if _settings.otel_traces_export if export is None else export:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=_settings.otel_exporter_otlp_endpoint, insecure=True))
        )
    trace.set_tracer_provider(provider)
    return provider
