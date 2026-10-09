"""Alembic environment. The database URL comes from DATABASE_URL.

Autogenerate compares the models with the database. The graph checkpointer's
tables have no model (see ``checkpoint_schema.py``), so they are left out of
the comparison; otherwise every new migration would propose dropping them.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import create_engine, pool

from openmarketer_core.db import models  # noqa: F401  (registers the tables on Base.metadata)
from openmarketer_core.db.base import Base
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url


def _is_ours(name: str | None, type_: NameFilterType, parent_names: NameFilterParentNames) -> bool:
    """Whether autogenerate should look at a database object: all but the checkpointer's."""
    table = name if type_ == "table" else parent_names.get("table_name")
    return table not in CHECKPOINT_TABLES


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        include_name=_is_ours,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_name=_is_ours
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
