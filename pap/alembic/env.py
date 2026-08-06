"""
Alembic environment — RAI Trust Platform PAP.

Supports both offline (SQL generation) and online (live DB) migration modes.
render_as_batch=True is required for SQLite ALTER TABLE support.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# Add pap/ to sys.path so model imports resolve correctly when running
# `alembic` from the pap/ directory or from the project root.
_PAP_DIR = Path(__file__).resolve().parent.parent
if str(_PAP_DIR) not in sys.path:
    sys.path.insert(0, str(_PAP_DIR))

# Register all ORM models with Base.metadata
from core.database import Base  # noqa: E402
from core import models as _models  # noqa: E402, F401

config = context.config

# Override URL from environment if set (matches runtime database.py logic)
_db_url = os.getenv("PAP_DATABASE_URL", config.get_main_option("sqlalchemy.url"))
config.set_main_option("sqlalchemy.url", _db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL script without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,       # SQLite ALTER TABLE support
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,   # SQLite ALTER TABLE support
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
