"""
Pack Manager -- CRUD operations on policy pack definitions.

Mutations write to the shared PAP SQLite database (config_policy_packs,
config_pack_dimensions, and the pe/policy_packs service-config blob).
After each write the PackLoader is hot-reloaded from DB so changes take
effect immediately without a service restart.

YAML fallback: when DB is unavailable, mutations fall back to writing
policy_packs.yaml (legacy path) so the API remains functional in
offline/dev environments.

Constraints:
  - Pack IDs must be lowercase alphanumeric/underscore, 2-64 chars.
  - Packs with layer='mandatory' cannot be deleted (system-protected).
  - All other packs can be created, updated, or deleted freely.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

import yaml

from engine.pack_loader import Pack, PackLoader

logger = logging.getLogger(__name__)

_VALID_ID = re.compile(r'^[a-z][a-z0-9_]{1,63}$')


class PackManager:
    """
    Wraps PackLoader with write capabilities.

    Every mutation writes to DB (preferred) or YAML (fallback) then
    hot-reloads the PackLoader so the running engine sees the updated state.
    """

    def __init__(self, pack_loader: PackLoader, packs_path: str):
        self._loader     = pack_loader
        self._packs_path = Path(packs_path)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _current_raw(self) -> dict:
        """
        Return current pack state as a plain dict built from the loader's
        in-memory state (which is DB-backed).  This avoids reading stale YAML.
        """
        raw_packs: dict = {}
        for pack_id, pack in self._loader._packs.items():
            raw_packs[pack_id] = {
                "layer":               pack.layer,
                "enforcement":         pack.enforcement,
                "mode":                pack.mode,
                "combining_algorithm": pack.combining_algorithm,
                "hitl_trigger":        pack.hitl_trigger,
                "version":             pack.version,
                "owner":               pack.owner,
                "description":         pack.description,
                "active":              pack.active,
                "dimensions": [
                    {
                        "id":       d.id,
                        "profile":  d.profile,
                        "severity": d.severity,
                        "abort":    d.abort,
                        "roles":    list(d.roles),
                        "note":     d.note,
                    }
                    for d in pack.dimensions
                ],
                "context_adjustments": [
                    {
                        "signal":   a.signal,
                        "operator": a.operator,
                        "value":    a.value,
                        "step":     a.step,
                        "affects":  list(a.affects),
                    }
                    for a in pack.context_adjustments
                ],
            }
        return {"packs": raw_packs}

    # ── DB write helpers ──────────────────────────────────────────────────────

    def _db_upsert_pack(self, pack_id: str, definition: dict) -> bool:
        """
        Write a pack definition to DB (config_policy_packs, config_pack_dimensions,
        and the pe/policy_packs service-config blob).

        Returns True on success, False if DB unavailable.
        """
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False
            from sqlalchemy import text
        except Exception:
            return False

        try:
            with engine.begin() as conn:
                # 1. Upsert pack header row
                conn.execute(
                    text("""
                        INSERT INTO config_policy_packs (
                            pack_id, display_name, description, layer, enforcement,
                            mode, combining_algorithm, hitl_trigger, owner, version,
                            active, created_at, updated_at
                        ) VALUES (
                            :pack_id, :display_name, :description, :layer, :enforcement,
                            :mode, :combining_algorithm, :hitl_trigger, :owner, :version,
                            :active, datetime('now'), datetime('now')
                        ) ON CONFLICT(pack_id) DO UPDATE SET
                            display_name        = :display_name,
                            description         = :description,
                            layer               = :layer,
                            enforcement         = :enforcement,
                            mode                = :mode,
                            combining_algorithm = :combining_algorithm,
                            hitl_trigger        = :hitl_trigger,
                            owner               = :owner,
                            version             = :version,
                            active              = :active,
                            updated_at          = datetime('now')
                    """),
                    {
                        "pack_id":            pack_id,
                        "display_name":       definition.get("display_name", pack_id),
                        "description":        definition.get("description") or "",
                        "layer":              definition.get("layer", "domain-addon"),
                        "enforcement":        definition.get("enforcement", "flexible"),
                        "mode":               definition.get("mode", "collect-all"),
                        "combining_algorithm": definition.get("combining_algorithm", "worst_case"),
                        "hitl_trigger":       int(bool(definition.get("hitl_trigger", False))),
                        "owner":              definition.get("owner") or "",
                        "version":            definition.get("version", "1.0.0"),
                        "active":             int(bool(definition.get("active", True))),
                    },
                )

                # 2. Replace all dimension rows for this pack
                conn.execute(
                    text("DELETE FROM config_pack_dimensions WHERE pack_id = :pid"),
                    {"pid": pack_id},
                )
                for dim in definition.get("dimensions", []):
                    if not isinstance(dim, dict) or not dim.get("id"):
                        continue
                    conn.execute(
                        text("""
                            INSERT INTO config_pack_dimensions (
                                pack_id, dimension_id, profile_id, severity, abort,
                                active_roles, note, created_at, updated_at
                            ) VALUES (
                                :pack_id, :dim_id, :profile_id, :severity, :abort,
                                :active_roles, :note, datetime('now'), datetime('now')
                            )
                        """),
                        {
                            "pack_id":      pack_id,
                            "dim_id":       dim["id"],
                            "profile_id":   dim.get("profile", "standard"),
                            "severity":     dim.get("severity", 2),
                            "abort":        int(bool(dim.get("abort", False))),
                            "active_roles": json.dumps(dim.get("roles") or []),
                            "note":         dim.get("note") or "",
                        },
                    )

                # 3. Sync the pe/policy_packs service-config blob (stores context_adjustments)
                row = conn.execute(
                    text(
                        "SELECT config_json FROM config_service_configs "
                        "WHERE service_name = 'pe' AND config_key = 'policy_packs'"
                    )
                ).fetchone()
                if row:
                    blob = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                    blob.setdefault("packs", {})[pack_id] = definition
                    conn.execute(
                        text(
                            "UPDATE config_service_configs "
                            "SET config_json = :cfg, updated_at = datetime('now') "
                            "WHERE service_name = 'pe' AND config_key = 'policy_packs'"
                        ),
                        {"cfg": json.dumps(blob)},
                    )

            logger.info(
                f"[PackManager] DB write: upserted pack '{pack_id}' with "
                f"{len(definition.get('dimensions', []))} dimensions"
            )
            return True

        except Exception as exc:
            logger.warning(f"[PackManager] DB write failed ({exc}) -- falling back to YAML")
            return False

    def _db_delete_pack(self, pack_id: str) -> bool:
        """
        Remove a pack from DB tables and the service-config blob.
        Returns True on success, False if DB unavailable.
        """
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False
            from sqlalchemy import text
        except Exception:
            return False

        try:
            with engine.begin() as conn:
                # CASCADE deletes config_pack_dimensions automatically
                conn.execute(
                    text("DELETE FROM config_policy_packs WHERE pack_id = :pid"),
                    {"pid": pack_id},
                )
                row = conn.execute(
                    text(
                        "SELECT config_json FROM config_service_configs "
                        "WHERE service_name = 'pe' AND config_key = 'policy_packs'"
                    )
                ).fetchone()
                if row:
                    blob = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                    blob.get("packs", {}).pop(pack_id, None)
                    conn.execute(
                        text(
                            "UPDATE config_service_configs "
                            "SET config_json = :cfg, updated_at = datetime('now') "
                            "WHERE service_name = 'pe' AND config_key = 'policy_packs'"
                        ),
                        {"cfg": json.dumps(blob)},
                    )
            logger.info(f"[PackManager] DB write: deleted pack '{pack_id}'")
            return True
        except Exception as exc:
            logger.warning(f"[PackManager] DB delete failed ({exc}) -- falling back to YAML")
            return False

    def _db_toggle_active(self, pack_id: str, new_state: bool) -> bool:
        """Toggle active flag in DB. Returns True on success."""
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE config_policy_packs "
                        "SET active = :active, updated_at = datetime('now') "
                        "WHERE pack_id = :pid"
                    ),
                    {"active": int(new_state), "pid": pack_id},
                )
            logger.info(f"[PackManager] DB write: pack '{pack_id}' active -> {new_state}")
            return True
        except Exception as exc:
            logger.warning(f"[PackManager] DB toggle failed ({exc}) -- falling back to YAML")
            return False

    # ── YAML fallback (used only when DB is unavailable) ──────────────────────

    def _yaml_read_raw(self) -> dict:
        """Load current YAML from disk as a plain dict (DB-unavailable fallback)."""
        raw = yaml.safe_load(self._packs_path.read_text(encoding="utf-8")) or {}
        if "packs" not in raw or not isinstance(raw["packs"], dict):
            raw["packs"] = {}
        return raw

    def _yaml_commit(self, raw: dict) -> None:
        """Write updated dict to disk and hot-reload the PackLoader from YAML."""
        header = (
            "# Policy Packs -- RAI Trust Platform\n"
            "# Managed by Pack Manager (PAP API). Last written at runtime.\n\n"
        )
        content = header + yaml.dump(
            raw,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        self._packs_path.write_text(content, encoding="utf-8")
        self._loader._load_packs(str(self._packs_path))
        logger.info(
            f"[PackManager] YAML fallback commit -- "
            f"{len(self._loader._packs)} packs active"
        )

    # ── Validation helpers ────────────────────────────────────────────────────

    @staticmethod
    def _validate_id(pack_id: str) -> None:
        if not _VALID_ID.match(pack_id):
            raise ValueError(
                f"Invalid pack ID '{pack_id}': must be lowercase letters/digits/underscores, "
                f"2-64 chars, starting with a letter."
            )

    def _check_dim_overlap(self, pack_id: str, definition: dict) -> list:
        """
        Return list of strings describing dimension IDs in `definition`
        that already appear in another pack.  Enforces one-evaluator-per-pack.
        """
        current = self._current_raw()
        new_dim_ids = {
            d["id"] for d in definition.get("dimensions", [])
            if isinstance(d, dict) and d.get("id")
        }
        conflicts = []
        for pid, pdef in current["packs"].items():
            if pid == pack_id:
                continue   # skip self on update
            if not isinstance(pdef, dict):
                continue
            for d in pdef.get("dimensions", []):
                if isinstance(d, dict) and d.get("id") in new_dim_ids:
                    conflicts.append(f"{d['id']} (already in '{pid}')")
        return conflicts

    # ── Public API ────────────────────────────────────────────────────────────

    def create_pack(self, pack_id: str, definition: dict) -> Pack:
        """
        Create a new pack from a definition dict.
        Raises ValueError if the ID is invalid, already exists, or has overlapping evaluators.
        """
        self._validate_id(pack_id)
        conflicts = self._check_dim_overlap(pack_id, definition)
        if conflicts:
            raise ValueError(
                f"Evaluator overlap: the following evaluators already belong to another pack "
                f"and cannot be added here: {', '.join(conflicts)}"
            )

        current = self._current_raw()
        if pack_id in current["packs"]:
            raise ValueError(
                f"Pack '{pack_id}' already exists. "
                f"Use PUT /internal/packs/{pack_id} to update."
            )

        # Write to DB; fall back to YAML if DB unavailable
        if not self._db_upsert_pack(pack_id, definition):
            raw = self._yaml_read_raw()
            raw["packs"][pack_id] = definition
            self._yaml_commit(raw)
        else:
            self._loader.reload()

        pack = self._loader.get_pack(pack_id)
        if pack is None:
            raise RuntimeError(
                f"Pack '{pack_id}' was written but failed to parse. "
                f"Check dimension IDs and profile names."
            )
        logger.info(f"[PackManager] Created pack '{pack_id}' with {len(pack.dimensions)} dims")
        return pack

    def update_pack(self, pack_id: str, definition: dict) -> Pack:
        """
        Replace an existing pack's definition.
        Raises KeyError if not found; ValueError on evaluator overlap.
        """
        self._validate_id(pack_id)
        conflicts = self._check_dim_overlap(pack_id, definition)
        if conflicts:
            raise ValueError(
                f"Evaluator overlap: the following evaluators already belong to another pack "
                f"and cannot be added here: {', '.join(conflicts)}"
            )

        current = self._current_raw()
        if pack_id not in current["packs"]:
            raise KeyError(f"Pack '{pack_id}' not found.")

        # Preserve active flag if not explicitly supplied in the new definition
        if "active" not in definition:
            definition["active"] = current["packs"][pack_id].get("active", True)

        if not self._db_upsert_pack(pack_id, definition):
            raw = self._yaml_read_raw()
            if pack_id not in raw["packs"]:
                raise KeyError(f"Pack '{pack_id}' not found in YAML fallback.")
            if "active" not in definition:
                definition["active"] = raw["packs"][pack_id].get("active", True)
            raw["packs"][pack_id] = definition
            self._yaml_commit(raw)
        else:
            self._loader.reload()

        pack = self._loader.get_pack(pack_id)
        if pack is None:
            raise RuntimeError(
                f"Pack '{pack_id}' was written but failed to parse. "
                f"Check dimension IDs and profile names."
            )
        logger.info(f"[PackManager] Updated pack '{pack_id}' -- {len(pack.dimensions)} dims")
        return pack

    def delete_pack(self, pack_id: str) -> None:
        """
        Delete a pack.
        Raises KeyError if not found; ValueError if it is a mandatory pack.
        """
        current = self._current_raw()
        if pack_id not in current["packs"]:
            raise KeyError(f"Pack '{pack_id}' not found.")
        if current["packs"][pack_id].get("layer") == "mandatory":
            raise ValueError(
                f"Pack '{pack_id}' is a mandatory system pack and cannot be deleted. "
                f"Edit its dimensions instead."
            )

        if not self._db_delete_pack(pack_id):
            raw = self._yaml_read_raw()
            if pack_id not in raw["packs"]:
                raise KeyError(f"Pack '{pack_id}' not found in YAML fallback.")
            del raw["packs"][pack_id]
            self._yaml_commit(raw)
        else:
            self._loader.reload()

        logger.info(f"[PackManager] Deleted pack '{pack_id}'")

    def toggle_pack_active(self, pack_id: str) -> bool:
        """
        Toggle the active flag on a pack (true -> false or false -> true).
        Mandatory packs cannot be disabled.
        Returns the new active state.
        """
        current = self._current_raw()
        if pack_id not in current["packs"]:
            raise KeyError(f"Pack '{pack_id}' not found.")
        pdef = current["packs"][pack_id]
        if pdef.get("layer") == "mandatory":
            raise ValueError(
                f"Pack '{pack_id}' is a mandatory system pack and cannot be disabled."
            )

        new_state = not bool(pdef.get("active", True))

        if not self._db_toggle_active(pack_id, new_state):
            # YAML fallback
            raw = self._yaml_read_raw()
            if pack_id not in raw["packs"]:
                raise KeyError(f"Pack '{pack_id}' not found in YAML fallback.")
            raw["packs"][pack_id]["active"] = new_state
            self._yaml_commit(raw)
        else:
            self._loader.reload()

        logger.info(f"[PackManager] Pack '{pack_id}' active -> {new_state}")
        return new_state
