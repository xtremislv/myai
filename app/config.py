"""Centralized settings loaded from environment / .env."""
from __future__ import annotations

import base64
import binascii
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    app_name: str = "ai-platform"
    environment: str = "dev"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- auth ---
    jwt_secret: str = Field(default="change-me-in-prod")
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 3600

    # --- LLM (vLLM, OpenAI-compatible) ---
    llm_base_url: str = "http://vllm:8001/v1"
    llm_api_key: str = "not-needed-for-local"
    # Default: a 26B / A4B MoE model (~4B active params per token).
    # Switch to NVIDIA Nemotron by overriding LLM_MODEL in .env (see .env.example).
    llm_model: str = "google/gemma-4-26B-A4B-it"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    # The model server's context window (vLLM --max-model-len). Prompts are fitted into it,
    # oldest conversation turns first, so a long chat never fails for being too long.
    llm_context_tokens: int = 65536
    # Agents are told the current date and time in this zone ("tomorrow" must mean something).
    timezone: str = "Asia/Kolkata"

    # --- Web access for agents (app/browsing) ---
    # Bing market for search and news, e.g. en-IN, en-US.
    search_market: str = "en-IN"
    # Private temporary folder for long jobs (summaries of data bigger than the context).
    work_dir: str = "/var/lib/ai-platform/work"
    # Published Android app: twai.apk + release.json, written by scripts/publish_android.sh.
    android_release_dir: str = "/var/lib/ai-platform/android"
    # Most characters of one page or tool result handed to the model.
    web_max_chars: int = 20000
    # Route the agents' web traffic (pages, search APIs, Chromium) through this proxy, e.g.
    # http://user:pass@host:3128 or socks5h://host:1080. Empty = direct. The model server, mail and
    # internal services never use it. The network guard still applies (see netguard).
    outbound_proxy: str = ""
    # Headless Chromium sessions: one per person, closed after this many idle seconds.
    browser_max_sessions: int = 6
    browser_idle_seconds: int = 600

    # --- Postgres ---
    postgres_dsn: str = "postgresql://ai:ai@postgres:5432/ai_platform"

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"

    # --- MinIO / object storage ---
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ai-platform"
    minio_secure: bool = False
    attachment_max_bytes: int = 10 * 1024 * 1024

    # --- Vault (envelope encryption) ---
    # 32 raw bytes, base64-encoded. Generate: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    vault_master_key: str = Field(default="")

    # --- Observability ---
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "ai-platform-gateway"
    # Send traces to the collector. Off on native hosts: Alloy has no trace backend and rejects
    # them, so the exporter would log a failure every minute.
    otel_traces_export: bool = True
    metrics_enabled: bool = True
    # A separate port that is never published, so metrics can't be reached from the internet.
    metrics_port: int = 9100

    # --- Ops alerts: Prometheus posts to /v1/ops/alerts/api/v2/alerts with this bearer token ---
    alert_webhook_token: str = ""

    # --- GPU fairness: private chat replies in progress ---
    private_max_per_user: int = 2
    private_max_total: int = 16


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Values shipped in code or in .env.example: public, so never valid as secrets.
_PUBLIC_JWT_SECRETS = {"change-me-in-prod", "please-change-this-to-a-long-random-string"}
_MIN_JWT_SECRET_LENGTH = 32


def secret_problems(settings: Settings) -> list[str]:
    """Why these settings are unsafe to boot with. Messages never include values."""
    problems = []

    if settings.jwt_secret in _PUBLIC_JWT_SECRETS or len(settings.jwt_secret) < _MIN_JWT_SECRET_LENGTH:
        problems.append(
            f"JWT_SECRET must be a private random string of at least {_MIN_JWT_SECRET_LENGTH} "
            "characters: with a known secret, anyone can forge an admin token"
        )

    try:
        key_length = len(base64.b64decode(settings.vault_master_key, validate=True))
    except (binascii.Error, ValueError):
        key_length = 0
    if key_length != 32:
        problems.append("VAULT_MASTER_KEY must be 32 random bytes, base64-encoded")

    # Dev runs against the throwaway defaults in docker-compose.yml.
    if settings.environment != "dev":
        if settings.minio_secret_key == "minioadmin":
            problems.append("MINIO_SECRET_KEY is still the default")
        if ":ai@" in settings.postgres_dsn:
            problems.append("POSTGRES_DSN is still using the default password")

    return problems


def validate_secrets(settings: Settings) -> None:
    problems = secret_problems(settings)
    if problems:
        raise RuntimeError("Refusing to start:\n  - " + "\n  - ".join(problems))
