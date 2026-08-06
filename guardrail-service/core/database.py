"""
Guardrail Service — database module.

Connects to the shared PAP SQLite database (same file as Policy Engine and PAP).
Set PAP_DB_PATH (or AUDIT_SQLITE_PATH as fallback) in guardrail-service/.env to
point at the shared pap.db file.

Provides:
  GS_DB_AVAILABLE  — True when a valid DB path is configured
  engine           — SQLAlchemy engine (or None when unavailable)
  query_service_config(service_name, config_key) → dict | None
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Accept either PAP_DB_PATH (preferred) or AUDIT_SQLITE_PATH (shared with PE)
_DB_PATH: str = (
    os.getenv("PAP_DB_PATH", "").strip()
    or os.getenv("AUDIT_SQLITE_PATH", "").strip()
)

GS_DB_AVAILABLE: bool = bool(_DB_PATH)

engine = None

if GS_DB_AVAILABLE:
    try:
        from sqlalchemy import create_engine as _create_engine
        engine = _create_engine(
            f"sqlite:///{_DB_PATH}",
            connect_args={"check_same_thread": False},
        )
        logger.info(f"[GS-DB] Connected to PAP DB: {_DB_PATH}")
    except Exception as _e:
        logger.warning(f"[GS-DB] Failed to connect to PAP DB ({_e}) — DB features disabled")
        GS_DB_AVAILABLE = False
        engine = None
else:
    logger.info(
        "[GS-DB] PAP_DB_PATH / AUDIT_SQLITE_PATH not set — "
        "Guardrail Service will use YAML fallbacks for all config"
    )


def query_service_config(service_name: str, config_key: str) -> Optional[dict]:
    """
    Retrieve a service config blob from config_service_configs.

    Returns the parsed dict on success, or None if the row is absent,
    the DB is unavailable, or any error occurs.
    """
    if not GS_DB_AVAILABLE or engine is None:
        return None
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT config_json FROM config_service_configs "
                    "WHERE service_name = :sn AND config_key = :ck"
                ),
                {"sn": service_name, "ck": config_key},
            ).fetchone()
        if row is None:
            return None
        data = row[0]
        if isinstance(data, str):
            data = json.loads(data)
        return data
    except Exception as exc:
        logger.warning(
            f"[GS-DB] query_service_config({service_name!r}, {config_key!r}) failed: {exc}"
        )
        return None


def write_provider_alert(provider_name: str, reason: str) -> None:
    """
    Write a PROVIDER_UNAVAILABLE alert to platform_alerts.
    Called by the SPI base class whenever a provider fails.
    Best-effort — silently swallows all errors so the GS never crashes.
    """
    if not GS_DB_AVAILABLE or engine is None:
        return
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO platform_alerts
                        (category, severity, provider_name, payload, triggered_at)
                    VALUES
                        ('PROVIDER_UNAVAILABLE', 'HIGH', :pname, :payload, datetime('now'))
                """),
                {
                    "pname":   provider_name,
                    "payload": json.dumps({
                        "provider": provider_name,
                        "reason":   reason,
                    }),
                },
            )
        logger.info(f"[GS-DB] provider alert written for '{provider_name}'")
    except Exception as exc:
        logger.warning(f"[GS-DB] write_provider_alert failed: {exc}")


def query_provider_config(provider_id: str) -> Optional[dict]:
    """
    Return the config_providers row for provider_id as a plain dict,
    or None if not found / DB unavailable.

    Returned dict keys match the config_providers column names:
      provider_id, provider_type, active, endpoint, api_key_env_var,
      extra_config, supported_dims, score_mapping, category_mapping, fail_mode
    """
    if not GS_DB_AVAILABLE or engine is None:
        return None
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT provider_id, provider_type, active, endpoint,
                           api_key_env_var, extra_config, supported_dims,
                           score_mapping, category_mapping, fail_mode
                    FROM config_providers
                    WHERE provider_id = :pid
                """),
                {"pid": provider_id},
            ).fetchone()
        if row is None:
            return None

        def _parse(val):
            if val is None:
                return None
            if isinstance(val, (dict, list)):
                return val
            try:
                return json.loads(val)
            except Exception:
                return val

        keys = [
            "provider_id", "provider_type", "active", "endpoint",
            "api_key_env_var", "extra_config", "supported_dims",
            "score_mapping", "category_mapping", "fail_mode",
        ]
        result = dict(zip(keys, row))
        for json_key in ("extra_config", "supported_dims", "score_mapping", "category_mapping"):
            result[json_key] = _parse(result.get(json_key))
        return result
    except Exception as exc:
        logger.warning(f"[GS-DB] query_provider_config({provider_id!r}) failed: {exc}")
        return None


def query_all_active_providers() -> Optional[list]:
    """
    Return all active config_providers rows as a list of dicts.
    Returns None if DB unavailable.
    """
    if not GS_DB_AVAILABLE or engine is None:
        return None
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT provider_id, provider_type, active, endpoint,
                           api_key_env_var, extra_config, supported_dims,
                           score_mapping, category_mapping, fail_mode
                    FROM config_providers
                    WHERE active = 1
                    ORDER BY provider_id
                """)
            ).fetchall()
        keys = [
            "provider_id", "provider_type", "active", "endpoint",
            "api_key_env_var", "extra_config", "supported_dims",
            "score_mapping", "category_mapping", "fail_mode",
        ]

        def _parse(val):
            if val is None:
                return None
            if isinstance(val, (dict, list)):
                return val
            try:
                return json.loads(val)
            except Exception:
                return val

        result = []
        for row in rows:
            d = dict(zip(keys, row))
            for json_key in ("extra_config", "supported_dims", "score_mapping", "category_mapping"):
                d[json_key] = _parse(d.get(json_key))
            result.append(d)
        return result
    except Exception as exc:
        logger.warning(f"[GS-DB] query_all_active_providers failed: {exc}")
        return None


def update_provider_health(
    provider_id: str,
    health_status: str,
    consecutive_failures: int,
    last_failure_reason: Optional[str] = None,
) -> None:
    """
    Update health tracking columns for a provider after each evaluation call.
    Best-effort — silently swallows errors.
    """
    if not GS_DB_AVAILABLE or engine is None:
        return
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE config_providers
                    SET health_status          = :status,
                        consecutive_failures   = :failures,
                        last_failure_reason    = :reason,
                        last_checked_at        = datetime('now'),
                        updated_at             = datetime('now')
                    WHERE provider_id = :pid
                """),
                {
                    "status":   health_status,
                    "failures": consecutive_failures,
                    "reason":   last_failure_reason,
                    "pid":      provider_id,
                },
            )
    except Exception as exc:
        logger.warning(f"[GS-DB] update_provider_health({provider_id!r}) failed: {exc}")


def query_evaluation_dimensions() -> Optional[list]:
    """
    Return all rows from config_evaluation_dimensions, or None on failure.
    Each row is a dict with keys: dimension_id, category, provider, status,
    supported_roles, requires_context.
    """
    if not GS_DB_AVAILABLE or engine is None:
        return None
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT dimension_id, category, provider, status, "
                    "       supported_roles, requires_context "
                    "FROM config_evaluation_dimensions"
                )
            ).fetchall()
        if not rows:
            return None
        result = []
        for row in rows:
            roles_raw = row[4]
            if isinstance(roles_raw, str):
                try:
                    roles = json.loads(roles_raw)
                except Exception:
                    roles = []
            else:
                roles = list(roles_raw or [])
            result.append({
                "dimension_id":    row[0],
                "category":        row[1],
                "provider":        row[2],
                "status":          row[3],
                "supported_roles": roles,
                "requires_context": bool(row[5]),
            })
        return result
    except Exception as exc:
        logger.warning(f"[GS-DB] query_evaluation_dimensions failed: {exc}")
        return None
