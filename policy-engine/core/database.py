"""
Policy Engine — SQLAlchemy connection to the shared PAP database.

The PE connects to the same SQLite file as the PAP (AUDIT_SQLITE_PATH) so that:
  - Config tables (config_app_pack_assignments, config_policies, config_threshold_profiles,
    config_policy_packs, config_session_policy, config_service_configs) are readable
    by the PE without any file synchronisation or polling.
  - Runtime PE tables (pe_sessions, pe_app_trust_scores) are written by the PE
    and readable by the PAP dashboard without any HTTP round-trip.

Set AUDIT_SQLITE_PATH in policy-engine/.env to the absolute path of pap/config/pap.db.
If AUDIT_SQLITE_PATH is not set, a PE-local fallback DB is used and YAML fallbacks
are activated for all config reads.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────

_DB_PATH: str = os.getenv("AUDIT_SQLITE_PATH", "").strip()

if _DB_PATH:
    DATABASE_URL = f"sqlite:///{_DB_PATH}"
    DB_AVAILABLE = True
    logger.debug(f"[PE DB] Using shared PAP database: {_DB_PATH}")
else:
    # Fallback — PE-local DB (config reads fall back to YAML)
    _FALLBACK = Path(__file__).parent.parent / "data" / "pe_local.db"
    _FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite:///{_FALLBACK}"
    DB_AVAILABLE = False
    logger.warning(
        "[PE DB] AUDIT_SQLITE_PATH not set — using local fallback DB. "
        "Config reads (pack assignments, policies) will fall back to YAML files."
    )

# ── Engine ──────────────────────────────────────────────────────────────────────

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


@event.listens_for(engine, "connect")
def _on_connect(conn, _record):
    conn.execute("PRAGMA foreign_keys = ON")
    # Env-driven journal mode — DELETE in the single-container deploy (the shared
    # pap.db is on Docker overlayfs, where WAL's -shm mmap corrupts across procs).
    conn.execute(f"PRAGMA journal_mode = {os.getenv('SQLITE_JOURNAL_MODE', 'WAL')}")
    conn.execute("PRAGMA busy_timeout = 5000")


# ── Session factory ─────────────────────────────────────────────────────────────

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class PeBase(DeclarativeBase):
    """Separate Base so PE-owned tables can be created with create_all without
    touching any PAP-owned tables."""
    pass


def get_session() -> Session:
    """Return a new DB session. Caller is responsible for closing it."""
    return SessionLocal()


def init_pe_tables() -> None:
    """
    Create PE-owned runtime tables (pe_sessions, pe_session_summaries,
    pe_app_trust_scores) if they don't already exist.

    Uses create_all which is idempotent — safe to call on every startup.
    Whichever service starts first (PAP or PE) creates the tables.
    """
    from core.pe_models import PeBase as _PeBase  # noqa: F401
    _PeBase.metadata.create_all(bind=engine)
    logger.info("[PE DB] PE runtime tables created/verified (create_all)")


def query_one(sql: str, params: dict | None = None):
    """Execute a raw SELECT and return the first row as a Row, or None."""
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return result.fetchone()


def query_all(sql: str, params: dict | None = None):
    """Execute a raw SELECT and return all rows as a list of Row objects."""
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return result.fetchall()


def execute_write(sql: str, params: dict | None = None) -> int:
    """Execute a raw INSERT/UPDATE/DELETE. Returns rowcount."""
    with engine.begin() as conn:
        result = conn.execute(text(sql), params or {})
        return result.rowcount
