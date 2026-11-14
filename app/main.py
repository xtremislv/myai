"""FastAPI application entrypoint: wiring, lifespan, observability, routes."""
from __future__ import annotations

import hashlib
import logging
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
from opentelemetry.instrumentation.dependencies import get_dependency_conflicts
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

from app import health
from app.api import (
    routes_admin,
    routes_auth,
    routes_browser,
    routes_chat,
    routes_dashboard,
    routes_devices,
    routes_me,
    routes_ops,
    routes_request,
    routes_scouts,
    routes_templates,
    routes_voice,
)
from app.config import get_settings, validate_secrets
from app.db.session import engine, init_db
from app.observability import setup_log_export, setup_logging, setup_tracing, start_metrics_server
from app.storage.objects import ensure_bucket

_settings = get_settings()
logger = logging.getLogger(__name__)


def instrument_sqlalchemy() -> bool:
    """Trace database calls, unless the installed SQLAlchemy is newer than the instrumentation supports:
    then it would do nothing but log an ERROR on every start."""
    instrumentor = SQLAlchemyInstrumentor()
    conflict = get_dependency_conflicts(instrumentor.instrumentation_dependencies())
    if conflict:
        logger.info("database tracing off: %s", conflict)
        return False
    instrumentor.instrument(engine=engine)
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_secrets(_settings)
    setup_logging()
    setup_log_export()
    setup_tracing()
    init_db()
    try:
        ensure_bucket()
    except Exception:  # MinIO may not be up yet in some dev flows; don't crash boot.
        pass
    instrument_sqlalchemy()
    metrics_server = start_metrics_server() if _settings.metrics_enabled else None
    # Background jobs a restart cut off: tell their owners (in the background: boot doesn't wait).
    import threading

    from app import jobs

    threading.Thread(target=jobs.report_interrupted, daemon=True, name="interrupted-jobs").start()
    yield
    if metrics_server is not None:
        metrics_server.shutdown()


# The interactive API docs are for development; in production they just map the API for outsiders.
_dev = _settings.environment == "dev"
app = FastAPI(
    title=_settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _dev else None,
    redoc_url="/redoc" if _dev else None,
    openapi_url="/openapi.json" if _dev else None,
)
FastAPIInstrumentor.instrument_app(app)

app.include_router(routes_auth.router)
app.include_router(routes_chat.router)
app.include_router(routes_admin.router)
app.include_router(routes_request.router)
app.include_router(routes_dashboard.router)
app.include_router(routes_me.router)
app.include_router(routes_ops.router)
app.include_router(routes_templates.router)
app.include_router(routes_browser.router)
app.include_router(routes_devices.router)
app.include_router(routes_scouts.router)
app.include_router(routes_voice.router)
WEB_DIR = Path(__file__).parent / "web"


def _ui_page() -> str:
    """index.html with app.js/style.css tagged by their content hash.

    Cloudflare tells browsers to keep static files for hours, so without the tag people kept the
    old code after an update. The page itself isn't cached, so a new tag reaches them at once.
    """
    page = (WEB_DIR / "index.html").read_text()
    for name in ("app.js", "style.css"):
        tag = hashlib.sha256((WEB_DIR / name).read_bytes()).hexdigest()[:12]
        page = page.replace(f'"{name}"', f'"{name}?v={tag}"')
    return page


@app.get("/ui/", include_in_schema=False)
@app.get("/ui/index.html", include_in_schema=False)
def ui_page() -> HTMLResponse:
    return HTMLResponse(_ui_page(), headers={"Cache-Control": "no-cache"})


app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")


@app.middleware("http")
async def ui_security_headers(request: Request, call_next):
    """The UI holds tokens that can open private chats, so it only runs its own files."""
    response = await call_next(request)
    if request.url.path.startswith("/ui"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
            "img-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-cache"
    return response


def _trace_id() -> str:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else uuid.uuid4().hex


@app.exception_handler(PermissionError)
async def permission_denied(request: Request, exc: PermissionError) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "trace_id": _trace_id()}, status_code=403)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Clients get a trace id, not internals. The log gets where it failed but not the
    exception's message, which could quote a private chat."""
    trace_id = _trace_id()
    logger.error(
        "unhandled %s on %s %s trace_id=%s\n%s",
        type(exc).__name__, request.method, request.url.path, trace_id,
        "".join(traceback.format_tb(exc.__traceback__)),
    )
    return JSONResponse(
        {"detail": "The server hit an unexpected error. Try again.", "trace_id": trace_id},
        status_code=500,
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


@app.get("/healthz")
def healthz() -> dict:
    """Is the process alive?"""
    return {"status": "ok", "service": _settings.otel_service_name}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Can it do its job? 503 until Postgres, Redis, object storage and the model are all up."""
    ready, checks = health.readiness()
    return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)
