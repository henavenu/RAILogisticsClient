"""
PAP database — SQLAlchemy engine, session factory, and Alembic migration runner.

Database: SQLite by default (config/rai_trust.db), swappable to Postgres via
  PAP_DATABASE_URL environment variable.  The connection string is the only
  change required to move between engines — all models use portable column types.

SQLite-specific setup
---------------------
  WAL mode      — allows concurrent reads while the PAP writes, needed when the
                  Policy Engine reads config tables at startup/reload.
  FK enforcement — SQLite supports foreign keys but enforces them only when
                  PRAGMA foreign_keys=ON is set per connection.  The event
                  listener below applies it automatically on every connection.

Alembic
-------
  Schema is managed by Alembic (pap/alembic/).  `run_migrations()` is called at
  startup and applies any pending revisions.  Do not use Base.metadata.create_all()
  for schema management — Alembic owns the DDL lifecycle.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

# Resolve paths relative to this file (pap/core/database.py → pap/) so the DB
# location is stable regardless of which directory uvicorn/pytest is invoked from.
_PAP_DIR  = Path(__file__).resolve().parent.parent   # …/pap/
_DATA_DIR = _PAP_DIR / "config"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Honour PAP_DATABASE_URL if set; resolve relative sqlite:/// paths against pap/
# so that  sqlite:///config/pap.db  always means  pap/config/pap.db.
_raw_db_url = os.getenv("PAP_DATABASE_URL", "")
if _raw_db_url.startswith("sqlite:///"):
    _path_str = _raw_db_url[len("sqlite:///"):]
    _db_path  = Path(_path_str)
    if not _db_path.is_absolute():
        _db_path = (_PAP_DIR / _db_path).resolve()
    DATABASE_URL = f"sqlite:///{_db_path}"
elif _raw_db_url:
    DATABASE_URL = _raw_db_url
else:
    DATABASE_URL = f"sqlite:///{_DATA_DIR / 'rai_trust.db'}"

_is_sqlite = "sqlite" in DATABASE_URL

# ── Engine ─────────────────────────────────────────────────────────────────────

# Pool sizing: the dashboard fires many concurrent polling fetches (cockpit,
# sentinel, agent-activity, eval-trail, registry…) against this single PAP
# worker. SQLAlchemy's default pool (5 + 10 overflow = 15) gets exhausted by a
# burst of those polls layered over slow SQLite reads, and every later request —
# even /v1/health — then blocks 30s on pool checkout and times out. With WAL +
# check_same_thread=False, concurrent SQLite *readers* are cheap, so we give the
# pool plenty of headroom and fail fast (pool_pre_ping recycles dead conns).
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_size=int(os.getenv("PAP_DB_POOL_SIZE", "20")),
    max_overflow=int(os.getenv("PAP_DB_MAX_OVERFLOW", "40")),
    pool_timeout=int(os.getenv("PAP_DB_POOL_TIMEOUT", "10")),
    pool_pre_ping=True,
    echo=False,
)


@event.listens_for(engine, "connect")
def _on_connect(conn, _record):
    """Apply SQLite pragmas on every new connection. No-op on Postgres."""
    if _is_sqlite:
        conn.execute("PRAGMA foreign_keys = ON")
        # Journal mode is env-driven. Default WAL (best concurrency on a normal
        # filesystem), but in the single-container deployment the shared pap.db
        # lives on Docker's overlayfs, where WAL's mmap'd -shm shared memory is
        # NOT supported across processes → "database disk image is malformed".
        # The entrypoint sets SQLITE_JOURNAL_MODE=DELETE there (rollback journal,
        # overlayfs-safe). busy_timeout lets concurrent writers retry instead of
        # erroring while one holds the write lock.
        conn.execute(f"PRAGMA journal_mode = {os.getenv('SQLITE_JOURNAL_MODE', 'WAL')}")
        conn.execute("PRAGMA busy_timeout = 5000")


# ── Session ────────────────────────────────────────────────────────────────────

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_alembic_migrations() -> None:
    """
    Apply all pending Alembic migrations via subprocess.

    Using subprocess instead of programmatic alembic import avoids the
    import-shadowing conflict caused by the local pap/alembic/ directory
    having the same name as the installed alembic package.

    Works for both scenarios:
      - Brand new install: runs all 18+ migrations from scratch
      - git pull:          runs only the delta (new migrations added since last pull)

    The subprocess inherits the current Python executable and working directory
    (expected to be the pap/ directory when main.py starts).
    """
    pap_dir = Path(__file__).parent.parent  # pap/ directory
    alembic_ini = pap_dir / "alembic.ini"

    if not alembic_ini.exists():
        logger.warning(
            f"[DB] alembic.ini not found at {alembic_ini} — skipping Alembic migrations. "
            "Run 'cd pap && alembic upgrade head' manually."
        )
        return

    # IMPORTANT — invoke the alembic *console-script executable*, NOT
    # `python -m alembic`.  The migrations live in pap/alembic/ (an empty
    # __init__.py makes it a package), so with cwd=pap/ the cwd is on sys.path[0]
    # and `python -m alembic` resolves to the LOCAL migrations directory instead
    # of the installed alembic package — failing with "'alembic' is a package and
    # cannot be directly executed".  The console-script exe runs with its own
    # sys.path (Scripts/ dir), so it is immune to this shadowing.  We locate it
    # next to the current interpreter and fall back to PATH, then to `-m alembic`.
    import shutil
    _scripts_dir = Path(sys.executable).parent
    _exe_name    = "alembic.exe" if os.name == "nt" else "alembic"
    _alembic_exe = _scripts_dir / _exe_name
    if _alembic_exe.exists():
        cmd = [str(_alembic_exe), "upgrade", "head"]
    elif shutil.which("alembic"):
        cmd = [shutil.which("alembic"), "upgrade", "head"]
    else:
        # Last resort — works only if cwd isn't shadowing the alembic package.
        cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(pap_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            out = result.stdout.strip() or "(no output)"
            logger.info(f"[DB] Alembic upgrade head completed: {out}")
        else:
            logger.error(
                f"[DB] Alembic upgrade head failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
    except subprocess.TimeoutExpired:
        logger.error("[DB] Alembic upgrade head timed out after 60 s — run manually")
    except FileNotFoundError:
        logger.error(
            "[DB] alembic not found in PATH — install with: pip install alembic. "
            "Run 'cd pap && alembic upgrade head' manually."
        )
    except Exception as exc:
        logger.error(f"[DB] Alembic migration error: {exc}")


def _reconcile_missing_columns() -> None:
    """
    Self-heal additive schema drift: add any columns that the ORM models define
    but the live tables are missing.

    Why this is needed
    ------------------
    Alembic owns the DDL lifecycle, but on a *ported / copied* machine the
    `alembic upgrade head` subprocess can fail or no-op (alembic missing from the
    venv, a stale DB stamped at head, an interrupted run). When that happens an
    existing table keeps its OLD column set, and `create_all()` will NOT add the
    newer columns because it only creates whole missing tables — never alters an
    existing one. Seeding then crashes with e.g.
        "table config_policies has no column named policy_code".

    This pass closes that gap idempotently: it compares each mapped table's live
    columns against the model and issues `ALTER TABLE … ADD COLUMN …` for any that
    are missing. All of our post-0001 migrations are additive nullable/defaulted
    columns, which SQLite's ADD COLUMN supports. It never drops or rewrites
    anything, so it is safe to run on every startup.
    """
    from sqlalchemy import inspect as _sa_inspect
    from core import models as _models  # noqa: F401 — ensure metadata is populated

    try:
        insp = _sa_inspect(engine)
        existing_tables = set(insp.get_table_names())
    except Exception as exc:
        logger.error(f"[DB] schema reconcile — could not inspect database: {exc}")
        return

    added = 0
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table — create_all() handles it
        try:
            live_cols = {c["name"] for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            if col.name in live_cols:
                continue
            try:
                col_type = col.type.compile(dialect=engine.dialect)
            except Exception:
                col_type = "TEXT"
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'
            # SQLite can only ADD a NOT NULL column if a default is supplied; for a
            # NOT NULL column without one, add it as nullable so existing rows stay
            # valid (the model still enforces NOT NULL on new inserts at the ORM layer).
            default_val = getattr(getattr(col, "default", None), "arg", None)
            if default_val is not None and not callable(default_val):
                ddl += f" DEFAULT {default_val!r}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added += 1
                logger.warning(
                    f"[DB] schema reconcile — added missing column "
                    f"{table.name}.{col.name} ({col_type})"
                )
            except Exception as exc:
                logger.error(
                    f"[DB] schema reconcile — failed to add {table.name}.{col.name}: {exc}"
                )
    if added:
        logger.info(f"[DB] schema reconcile — {added} missing column(s) added")


def init_db() -> None:
    """
    Initialise the database at startup.

    Two-phase approach:
      1. Run Alembic migrations (upgrade head) via subprocess — handles both
         brand-new installs and git-pull schema deltas automatically.
      2. create_all as a safety net — creates any tables that Alembic doesn't
         manage (e.g. new ORM models added before a migration is written).

    This covers both scenarios without requiring manual CLI steps:
      Brand new install : Alembic creates all tables from revision 0001→head
      git pull          : Alembic applies only new revisions (idempotent)
    """
    # Phase 1: Alembic migrations (schema changes, new columns, indexes)
    _run_alembic_migrations()

    # Phase 2: Safety net for unmanaged tables (idempotent, skip existing)
    from core import models as _models  # noqa: F401  — registers ORM classes
    Base.metadata.create_all(bind=engine)
    logger.info("[DB] Tables verified (create_all safety net)")

    # Phase 3: Self-heal additive column drift on existing tables. Handles ported /
    # copied databases where the Alembic subprocess failed or no-opped, leaving a
    # table without its newer columns (create_all never alters existing tables).
    _reconcile_missing_columns()
