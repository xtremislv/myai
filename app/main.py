"""FastAPI application entrypoint: wiring, lifespan, observability, routes."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api import routes_admin, routes_auth, routes_chat, routes_dashboard, routes_request
from app.config import get_settings, validate_secrets
from app.db.session import engine, init_db
from app.observability import setup_logging, setup_tracing
from app.storage.objects import ensure_bucket

_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_secrets(_settings)
    setup_logging()
    setup_tracing()
    init_db()
    try:
        ensure_bucket()
    except Exception:  # MinIO may not be up yet in some dev flows; don't crash boot.
        pass
    SQLAlchemyInstrumentor().instrument(engine=engine)
    yield


app = FastAPI(title=_settings.app_name, version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)

app.include_router(routes_auth.router)
app.include_router(routes_chat.router)
app.include_router(routes_admin.router)
app.include_router(routes_request.router)
app.include_router(routes_dashboard.router)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="ui")


@app.middleware("http")
async def ui_security_headers(request: Request, call_next):
    """The UI holds tokens that can open private chats, so it only runs its own files."""
    response = await call_next(request)
    if request.url.path.startswith("/ui"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": _settings.otel_service_name}


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
