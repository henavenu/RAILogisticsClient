"""
App Stack Manager — per-app pack assignments and dimension overrides.

Implementation:
  DbAppStackManager reads from config_app_pack_assignments and
  config_dimension_overrides in the shared PAP SQLite database.
  Write operations write to those same tables so the PAP UI and PE are
  always consistent — no file sync required.

  AUDIT_SQLITE_PATH must be set. The DB is the sole source of truth.

  No in-memory caching — each read queries the DB directly.  SQLite with
  WAL mode handles ~1000 indexed reads/s without contention; pack
  assignments change rarely so no caching is needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data classes (shared by both implementations) ─────────────────────────────

@dataclass
class DimOverride:
    """Per-dimension tuning applied on top of the global pack definition."""
    dimension_id: str
    profile:      Optional[str] = None
    severity:     Optional[int] = None


@dataclass
class PackAssignment:
    """One pack assigned to an app, with optional dimension overrides."""
    pack_id:   str
    active:    bool
    overrides: List[DimOverride] = field(default_factory=list)


@dataclass
class AppStack:
    """Full stack definition for one app."""
    app_id:      str
    assignments: Dict[str, PackAssignment]   # pack_id → PackAssignment


# ══════════════════════════════════════════════════════════════════════════════
#  DB-backed implementation (preferred)
# ══════════════════════════════════════════════════════════════════════════════

class DbAppStackManager:
    """
    App stack manager backed by the shared PAP SQLite database.

    Reads from config_app_pack_assignments + config_dimension_overrides.
    Write operations write to those same tables so the PAP UI and PE are
    always consistent — no file sync required.
    """

    def __init__(self):
        from core.database import engine
        self._engine = engine
        logger.info("[DbAppStackManager] Initialised — reading from PAP DB")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _assignments_for_app(self, app_id: str) -> Dict[str, PackAssignment]:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT pack_id, active FROM config_app_pack_assignments "
                    "WHERE app_id = :app_id"
                ),
                {"app_id": app_id},
            ).fetchall()
        if not rows:
            return {}
        result: Dict[str, PackAssignment] = {}
        for row in rows:
            pack_id = row[0]
            active  = bool(row[1])
            overrides = self._overrides_for(app_id, pack_id)
            result[pack_id] = PackAssignment(pack_id=pack_id, active=active, overrides=overrides)
        return result

    def _overrides_for(self, app_id: str, pack_id: str) -> List[DimOverride]:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT dimension_id, profile_id, severity "
                    "FROM config_dimension_overrides "
                    "WHERE app_id = :app_id AND pack_id = :pack_id AND active = 1"
                ),
                {"app_id": app_id, "pack_id": pack_id},
            ).fetchall()
        return [
            DimOverride(dimension_id=r[0], profile=r[1], severity=r[2])
            for r in rows
        ]

    def _all_app_ids(self) -> List[str]:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT app_id FROM config_app_pack_assignments")
            ).fetchall()
        return [r[0] for r in rows]

    # ── Read API (mirrors AppStackManager interface) ───────────────────────────

    def list_app_ids(self) -> List[str]:
        return self._all_app_ids()

    def has_stack(self, app_id: str) -> bool:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM config_app_pack_assignments "
                    "WHERE app_id = :app_id LIMIT 1"
                ),
                {"app_id": app_id},
            ).fetchone()
        return row is not None

    def get_stack(self, app_id: str) -> Optional[AppStack]:
        assignments = self._assignments_for_app(app_id)
        if not assignments:
            return None
        return AppStack(app_id=app_id, assignments=assignments)

    def get_assignments(self, app_id: str) -> List[PackAssignment]:
        return list(self._assignments_for_app(app_id).values())

    def get_active_pack_ids(self, app_id: str) -> List[str]:
        return [a.pack_id for a in self.get_assignments(app_id) if a.active]

    def get_override(self, app_id: str, pack_id: str, dim_id: str) -> Optional[DimOverride]:
        for ov in self._overrides_for(app_id, pack_id):
            if ov.dimension_id == dim_id:
                return ov
        return None

    def serialise(self) -> dict:
        result = {}
        for app_id in self._all_app_ids():
            stack = self.get_stack(app_id)
            if stack:
                result[app_id] = {
                    "assignments": {
                        pack_id: {
                            "pack_id": a.pack_id,
                            "active":  a.active,
                            "overrides": [
                                {k: v for k, v in {
                                    "dimension_id": o.dimension_id,
                                    "profile":      o.profile,
                                    "severity":     o.severity,
                                }.items() if v is not None}
                                for o in a.overrides
                            ],
                        }
                        for pack_id, a in stack.assignments.items()
                    }
                }
        return result

    # ── Write API ──────────────────────────────────────────────────────────────

    def assign_pack(self, app_id: str, pack_id: str, active: bool = True) -> None:
        from sqlalchemy import text
        with self._engine.begin() as conn:
            # Ensure app exists
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO config_apps "
                    "(app_id, display_name, base_tier, enforcement_mode, "
                    " dynamic_evaluation, active, created_at, updated_at) "
                    "VALUES (:app_id, :app_id, 'MEDIUM', 'BLOCK', 0, 1, "
                    "        datetime('now'), datetime('now'))"
                ),
                {"app_id": app_id},
            )
            # Upsert assignment
            conn.execute(
                text(
                    "INSERT INTO config_app_pack_assignments "
                    "  (app_id, pack_id, active, assigned_at, assigned_by) "
                    "VALUES (:app_id, :pack_id, :active, datetime('now'), 'pe-api') "
                    "ON CONFLICT(app_id, pack_id) DO UPDATE SET active = :active"
                ),
                {"app_id": app_id, "pack_id": pack_id, "active": 1 if active else 0},
            )
        logger.info(f"[DbAppStackManager] Assigned pack '{pack_id}' to app '{app_id}' (active={active})")

    def remove_pack(self, app_id: str, pack_id: str) -> None:
        from sqlalchemy import text
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "DELETE FROM config_app_pack_assignments "
                    "WHERE app_id = :app_id AND pack_id = :pack_id"
                ),
                {"app_id": app_id, "pack_id": pack_id},
            )
        if result.rowcount == 0:
            raise KeyError(f"Pack '{pack_id}' not assigned to app '{app_id}'")
        logger.info(f"[DbAppStackManager] Removed pack '{pack_id}' from app '{app_id}'")

    def toggle_pack(self, app_id: str, pack_id: str) -> bool:
        from sqlalchemy import text
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT active FROM config_app_pack_assignments "
                    "WHERE app_id = :app_id AND pack_id = :pack_id"
                ),
                {"app_id": app_id, "pack_id": pack_id},
            ).fetchone()
            if row is None:
                raise KeyError(f"Pack '{pack_id}' not assigned to app '{app_id}'")
            new_active = 0 if row[0] else 1
            conn.execute(
                text(
                    "UPDATE config_app_pack_assignments "
                    "SET active = :active "
                    "WHERE app_id = :app_id AND pack_id = :pack_id"
                ),
                {"app_id": app_id, "pack_id": pack_id, "active": new_active},
            )
        logger.info(f"[DbAppStackManager] Pack '{pack_id}' in app '{app_id}' → active={bool(new_active)}")
        return bool(new_active)

    def set_overrides(self, app_id: str, pack_id: str, overrides: List[dict]) -> None:
        from sqlalchemy import text
        # Verify assignment exists
        if not self.has_stack(app_id):
            raise KeyError(f"App '{app_id}' has no stack")
        with self._engine.begin() as conn:
            # Remove existing overrides for this app+pack
            conn.execute(
                text(
                    "DELETE FROM config_dimension_overrides "
                    "WHERE app_id = :app_id AND pack_id = :pack_id"
                ),
                {"app_id": app_id, "pack_id": pack_id},
            )
            # Insert new overrides
            for ov in overrides:
                dim_id = ov.get("dimension") or ov.get("dimension_id")
                if not dim_id:
                    continue
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO config_dimension_overrides "
                        "  (app_id, pack_id, dimension_id, profile_id, severity, "
                        "   abort, active, created_at, updated_at) "
                        "VALUES (:app_id, :pack_id, :dim_id, :profile, :severity, "
                        "        NULL, 1, datetime('now'), datetime('now'))"
                    ),
                    {
                        "app_id":   app_id,
                        "pack_id":  pack_id,
                        "dim_id":   dim_id,
                        "profile":  ov.get("profile"),
                        "severity": ov.get("severity"),
                    },
                )
        logger.info(f"[DbAppStackManager] Set {len(overrides)} override(s) for '{pack_id}' in '{app_id}'")

    def clear_overrides(self, app_id: str, pack_id: str) -> None:
        from sqlalchemy import text
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM config_dimension_overrides "
                    "WHERE app_id = :app_id AND pack_id = :pack_id"
                ),
                {"app_id": app_id, "pack_id": pack_id},
            )
        logger.info(f"[DbAppStackManager] Cleared overrides for '{pack_id}' in '{app_id}'")

    def propagate_overrides(self, from_app: str, pack_id: str, to_app_ids: List[str]) -> None:
        src_overrides = [
            {"dimension_id": o.dimension_id, "profile": o.profile, "severity": o.severity}
            for o in self._overrides_for(from_app, pack_id)
        ]
        if not src_overrides:
            raise KeyError(f"Source assignment '{from_app}/{pack_id}' not found or has no overrides")
        for target in to_app_ids:
            try:
                self.set_overrides(target, pack_id, src_overrides)
            except KeyError:
                logger.warning(f"[DbAppStackManager] Propagate skipped — '{pack_id}' not assigned to '{target}'")
        logger.info(f"[DbAppStackManager] Propagated overrides from '{from_app}' to {to_app_ids}")


# ── Public alias ──────────────────────────────────────────────────────────────
# chain_executor and other callers use the generic name `AppStackManager` for
# type annotations and Optional parameters.  Alias keeps both import styles
# working without requiring a separate ABC at this stage.
AppStackManager = DbAppStackManager
