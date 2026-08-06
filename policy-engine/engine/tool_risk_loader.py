"""
Tool Risk Loader -- reads tool risk registry from the shared PAP database
and exposes risk-level lookups for the Tier 0 session guard.

Load path:
  Reads config_service_configs('pe', 'tool_risk') blob.
  AUDIT_SQLITE_PATH must be set. The DB is the sole source of truth —
  there is no YAML fallback.

Risk levels
-----------
  HIGH   -- Irreversible / high-impact operations (funds movement, etc.).
            Session-blocked after a COERCIVE verdict on the tool.
  MEDIUM -- Stateful but reversible. Not session-blocked (default for unknowns).
  LOW    -- Read-only / informational. Never session-blocked.

Lookup is by tool_name only (app_id-scoped entries fall back to the same
string key -- app_id is informational metadata in the registry, not a lookup
dimension, to keep the guard simple and fast).
"""
from __future__ import annotations

import json
import logging
from typing import Dict

logger = logging.getLogger(__name__)

_VALID_LEVELS = {"HIGH", "MEDIUM", "LOW"}
_DEFAULT_RISK = "MEDIUM"


class ToolRiskLoader:
    """
    Loads tool risk registry from DB.

    DB is the sole source of truth — AUDIT_SQLITE_PATH must be set.
    Thread-safe for read-only access after construction.
    Call reload() to refresh from DB without restarting the PE.
    """

    def __init__(self):
        self._risks: Dict[str, str] = {}   # tool_name -> "HIGH"|"MEDIUM"|"LOW"
        self.load()

    # -- Internal helpers ------------------------------------------------------

    def _parse_tools(self, raw: dict) -> Dict[str, str]:
        """
        Parse the tools mapping (from YAML or JSON blob) into the internal
        risk-level dict.  Unknown risk_level values fall back to MEDIUM.
        """
        tools = raw.get("tools") or {}
        loaded: Dict[str, str] = {}
        for tool_name, cfg in tools.items():
            if not isinstance(cfg, dict):
                continue
            risk = str(cfg.get("risk_level", _DEFAULT_RISK)).upper()
            if risk not in _VALID_LEVELS:
                logger.warning(
                    f"ToolRiskLoader: unknown risk_level '{risk}' for tool "
                    f"'{tool_name}' -- using default '{_DEFAULT_RISK}'"
                )
                risk = _DEFAULT_RISK
            loaded[tool_name] = risk
        return loaded

    def _load_from_db(self) -> bool:
        """
        Load tool risk registry from the config_service_configs blob
        (service_name='pe', config_key='tool_risk').

        Returns True on success, False if DB unavailable or blob absent.
        """
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False
            from sqlalchemy import text
        except Exception:
            return False

        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT config_json FROM config_service_configs "
                        "WHERE service_name = 'pe' AND config_key = 'tool_risk'"
                    )
                ).fetchone()

            if not row:
                logger.debug(
                    "ToolRiskLoader: no tool_risk blob in config_service_configs "
                    "-- falling back to YAML"
                )
                return False

            raw = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            if not raw:
                return False

            self._risks = self._parse_tools(raw)
            logger.info(
                f"ToolRiskLoader: loaded {len(self._risks)} entries from DB "
                f"(config_service_configs pe/tool_risk) -- "
                f"HIGH={sum(1 for v in self._risks.values() if v == 'HIGH')} "
                f"MEDIUM={sum(1 for v in self._risks.values() if v == 'MEDIUM')} "
                f"LOW={sum(1 for v in self._risks.values() if v == 'LOW')}"
            )
            return True

        except Exception as exc:
            logger.error(f"ToolRiskLoader: DB load failed: {exc}")
            return False

    # -- Lifecycle -------------------------------------------------------------

    def load(self) -> None:
        """Load registry from DB. Raises RuntimeError if DB is unavailable."""
        if not self._load_from_db():
            raise RuntimeError(
                "ToolRiskLoader: Failed to load tool risk registry from DB. "
                "Ensure AUDIT_SQLITE_PATH is set and config_service_configs "
                "contains a pe/tool_risk blob."
            )

    def reload(self) -> None:
        """Hot-reload from DB. Raises RuntimeError if DB is unavailable."""
        self._risks = {}
        self.load()

    # -- Lookup API ------------------------------------------------------------

    def get_risk(self, tool_name: str) -> str:
        """
        Return the risk level for a tool.

        Falls back to MEDIUM for unregistered tools so unknown tools are
        treated with caution but not session-blocked.
        """
        return self._risks.get(tool_name, _DEFAULT_RISK)

    def is_high_risk(self, tool_name: str) -> bool:
        """True iff this tool is classified as HIGH risk."""
        return self.get_risk(tool_name) == "HIGH"

    def is_low_risk(self, tool_name: str) -> bool:
        """True iff this tool is classified as LOW risk."""
        return self.get_risk(tool_name) == "LOW"

    def as_catalog(self) -> list:
        """Serialisable catalog -- used by GET /internal/tool-risks."""
        return [
            {"tool_name": name, "risk_level": level}
            for name, level in sorted(self._risks.items())
        ]

    @property
    def entry_count(self) -> int:
        return len(self._risks)
