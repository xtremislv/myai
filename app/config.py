"""Centralized settings loaded from environment / .env."""
from __future__ import annotations

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
    llm_model: str = "gemma-3-26b-a4b-it"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024

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

    # --- Vault (envelope encryption) ---
    # 32 raw bytes, base64-encoded. Generate: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    vault_master_key: str = Field(default="")

    # --- Observability ---
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "ai-platform-gateway"
    metrics_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
