"""
Policy Loader — loads policy definitions and exposes per-dimension threshold rules.

Load path:
  Reads from config_policies in the shared PAP SQLite database.
  PAP's publish step marks policies as published; the PE's /internal/reload
  endpoint triggers PolicyRegistry.reload() which re-reads from DB.

  AUDIT_SQLITE_PATH must be set. The DB is the sole source of truth —
  there is no YAML fallback.

Each PolicyDefinition holds:
  - thresholds: permissive / normative / coercive score ranges
  - severity:   1 (low) | 2 (medium) | 3 (high) — used for MRA tiebreak
  - remediations: human-readable text per verdict level
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PolicyDefinition:

    def __init__(self, name: str, app_id: Optional[str], cfg: dict):
        self.name    = name
        self.app_id  = app_id          # None → global default
        self.severity: int = cfg.get("severity", 1)
        t = cfg.get("thresholds", {})
        self._permissive: List[float] = t.get("permissive", [0, 4])
        self._normative:  List[float] = t.get("normative",  [5, 6])
        self._coercive:   List[float] = t.get("coercive",   [7, 9])
        self._remediations: dict = cfg.get("remediations", {})

        # MessageWriter fields (migration 0017)
        self.policy_code:        Optional[str] = cfg.get("policy_code")
        self.normative_action:   str           = cfg.get("normative_action", "REVISION")
        self.normative_guidance: Optional[str] = cfg.get("normative_guidance")
        # Per-policy HITL (migration 0019): non-PERMISSIVE on this policy → human review.
        self.hitl_trigger:       bool          = bool(cfg.get("hitl_trigger", False))

    def verdict_for_score(self, score: float) -> str:
        if self._permissive[0] <= score <= self._permissive[1]:
            return "PERMISSIVE"
        if self._normative[0]  <= score <= self._normative[1]:
            return "NORMATIVE"
        if self._coercive[0]   <= score <= self._coercive[1]:
            return "COERCIVE"
        logger.warning(f"Policy '{self.name}': score {score} outside all ranges — PERMISSIVE")
        return "PERMISSIVE"

    def remediation(self, verdict: str) -> Optional[str]:
        return self._remediations.get(verdict.lower())

    def as_dict(self) -> dict:
        return {
            "name":       self.name,
            "app_id":     self.app_id,
            "severity":   self.severity,
            "thresholds": {
                "permissive": self._permissive,
                "normative":  self._normative,
                "coercive":   self._coercive,
            },
        }


class PolicyRegistry:
    """
    In-memory registry of active PolicyDefinitions.
    Keyed by (app_id, dimension); falls back to (None, dimension) global default.

    DB is the sole source of truth. AUDIT_SQLITE_PATH must be configured.
    """

    def __init__(self):
        self._policies: Dict[Tuple[Optional[str], str], PolicyDefinition] = {}
        # Dimensions whose policy is EXPLICITLY disabled (active=0). The chain executor
        # forces these to PERMISSIVE even inside packs, so disabling a policy actually
        # stops enforcement everywhere (not just for its standalone threshold band).
        self._disabled_dims: set = set()
        self.load()

    @property
    def disabled_dims(self) -> set:
        return self._disabled_dims

    def load(self):
        """Load policies from DB. Raises RuntimeError if DB is unavailable or empty."""
        if not self._load_from_db():
            raise RuntimeError(
                "[PolicyRegistry] Failed to load policies from DB. "
                "Ensure AUDIT_SQLITE_PATH is set and config_policies table is populated."
            )

    def _load_from_db(self) -> bool:
        """
        Load from config_policies in the shared PAP database.
        Returns True on success, False if DB unavailable or table empty.
        """
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False

            from sqlalchemy import text
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT dimension, app_id, severity, "
                        "       permissive_lo, permissive_hi, "
                        "       normative_lo,  normative_hi, "
                        "       coercive_lo,   coercive_hi, "
                        "       remediation_normative, remediation_coercive, "
                        "       policy_code, normative_action, normative_guidance, "
                        "       hitl_trigger "
                        "FROM config_policies "
                        "WHERE active = 1"
                    )
                ).fetchall()

            if not rows:
                logger.warning("[PolicyRegistry] DB has no active policies — falling back to YAML")
                return False

            new_policies: Dict[Tuple[Optional[str], str], PolicyDefinition] = {}
            for row in rows:
                dim, app_id = row[0], row[1]
                cfg = {
                    "severity": row[2],
                    "thresholds": {
                        "permissive": [row[3], row[4]],
                        "normative":  [row[5], row[6]],
                        "coercive":   [row[7], row[8]],
                    },
                    "remediations": {
                        k: v for k, v in {
                            "normative": row[9],
                            "coercive":  row[10],
                        }.items() if v
                    },
                    # MessageWriter fields (migration 0017)
                    "policy_code":        row[11],
                    "normative_action":   row[12] or "REVISION",
                    "normative_guidance": row[13],
                    # Per-policy HITL (migration 0019)
                    "hitl_trigger":       bool(row[14]),
                }
                pd = PolicyDefinition(name=dim, app_id=app_id, cfg=cfg)
                new_policies[(app_id, dim)] = pd

            self._policies = new_policies

            # Load the set of explicitly-disabled dimensions (active=0). A disabled
            # policy is a hard "do not enforce this dimension" override honoured by the
            # chain executor — so the PAP enable/disable toggle takes effect globally,
            # even for dimensions enforced via pack threshold profiles.
            with engine.connect() as conn:
                drows = conn.execute(
                    text("SELECT DISTINCT dimension FROM config_policies WHERE active = 0")
                ).fetchall()
            self._disabled_dims = {r[0] for r in drows}

            logger.info(
                f"[PolicyRegistry] Loaded {len(self._policies)} active policy definition(s) "
                f"+ {len(self._disabled_dims)} disabled dimension(s) from PAP DB"
            )
            return True

        except Exception as exc:
            logger.error(f"[PolicyRegistry] DB load failed: {exc}")
            return False

    def reload(self):
        """Hot-reload — replaces in-memory state atomically."""
        self.load()

    def get(self, dimension: str, app_id: Optional[str] = None) -> Optional[PolicyDefinition]:
        return (
            self._policies.get((app_id, dimension))
            or self._policies.get((None, dimension))
        )

    def evaluate(
        self,
        dimension: str,
        score: float,
        app_id: Optional[str] = None,
    ) -> Tuple[str, Optional[PolicyDefinition]]:
        policy = self.get(dimension, app_id)
        if not policy:
            logger.warning(f"No policy definition for '{dimension}' — defaulting to PERMISSIVE")
            return "PERMISSIVE", None
        return policy.verdict_for_score(score), policy

    def as_catalog(self) -> List[dict]:
        return [pd.as_dict() for pd in self._policies.values()]
