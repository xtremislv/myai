"""Boot-time secret validation, and migrations that match the models."""
from __future__ import annotations

import base64
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.config import Settings, secret_problems, validate_secrets
from app.db.session import Base

GOOD = {
    "jwt_secret": "x" * 40,
    "vault_master_key": base64.b64encode(os.urandom(32)).decode(),
    "minio_secret_key": "a-real-secret",
    "postgres_dsn": "postgresql://ai:a-real-secret@postgres:5432/ai_platform",
}


def _settings(**overrides):
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_good_secrets_pass():
    assert secret_problems(_settings(environment="production")) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"jwt_secret": ""},
        {"jwt_secret": "change-me-in-prod"},
        {"jwt_secret": "please-change-this-to-a-long-random-string"},
        {"jwt_secret": "short"},
        {"vault_master_key": ""},
        {"vault_master_key": "not base64!"},
        {"vault_master_key": base64.b64encode(b"too short").decode()},
    ],
)
def test_bad_core_secrets_abort_boot_in_every_environment(bad):
    with pytest.raises(RuntimeError, match="Refusing to start"):
        validate_secrets(_settings(environment="dev", **bad))


@pytest.mark.parametrize(
    "bad",
    [
        {"minio_secret_key": "minioadmin"},
        {"postgres_dsn": "postgresql://ai:ai@postgres:5432/ai_platform"},
    ],
)
def test_default_service_passwords_are_only_allowed_in_dev(bad):
    assert secret_problems(_settings(environment="dev", **bad)) == []
    assert secret_problems(_settings(environment="production", **bad))


def test_error_never_contains_the_secret_value():
    with pytest.raises(RuntimeError) as e:
        validate_secrets(_settings(jwt_secret="short-secret-value"))
    assert "short-secret-value" not in str(e.value)


# ---- migrations ----
@pytest.fixture
def alembic_cfg(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, url


def test_upgrade_from_empty_builds_every_table_and_matches_the_models(alembic_cfg):
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], "models changed without a migration: run alembic revision --autogenerate"


def test_downgrade_removes_every_table(alembic_cfg):
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert set(inspect(create_engine(url)).get_table_names()) == {"alembic_version"}
