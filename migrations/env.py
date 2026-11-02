"""Alembic environment. Uses the app's models and database settings.

A `sqlalchemy.url` set on the Alembic config overrides the settings (tests use
this to migrate a throwaway database).
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from app.db.session import Base, engine

config = context.config
override_url = config.get_main_option("sqlalchemy.url")


def include_name(name, type_, parent_names):
    # LangGraph owns its checkpoint tables in the same database; leave them alone.
    return type_ != "table" or name in Base.metadata.tables


def run_offline() -> None:
    url = override_url or engine.url.render_as_string(hide_password=False)
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True, include_name=include_name)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connectable = create_engine(override_url) if override_url else engine
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, include_name=include_name)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
