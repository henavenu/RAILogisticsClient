"""
Evaluation Registry — loads the dimension taxonomy and exposes the full catalog.

Single load path: PAP database only.

  DB path (only):
    Reads from config_evaluation_dimensions in the shared PAP SQLite database.
    Triggered when PAP_DB_PATH (or AUDIT_SQLITE_PATH) is set.
    If DB is unavailable the registry starts in *degraded mode* — all endpoints
    continue to respond (no sys.exit), but /v1/health returns 503 and
    /v1/trust/evaluate returns 503 with a clear config-error message.

Each dimension carries:
  - category    : safety | security | privacy | trust
  - status      : active | deferred
  - roles       : which ContentRole values are applicable
  - provider    : which registered provider handles this dimension
"""
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class DimensionSpec:
    """Metadata for a single evaluation dimension."""

    def __init__(self, name: str, category: str, spec: dict):
        self.name     = name
        self.category = category
        self.status   = spec.get("status", "active")        # active | deferred
        self.roles: Set[str] = set(spec.get("roles") or spec.get("supported_roles") or [])
        self.provider: Optional[str] = spec.get("provider")
        self.requires_context: bool = bool(spec.get("requires_context", False))


class EvaluationRegistry:
    """
    Central registry for the evaluation dimension taxonomy.
    Loaded once at startup; hot-reloadable by calling reload().

    Loads from the PAP database only.  If the DB is unavailable the registry
    enters *degraded mode*:
      - _startup_error is set to a human-readable message
      - _dimensions remains empty
      - is_healthy() returns False
      - /v1/health → 503,  /v1/trust/evaluate → 503
    No sys.exit — the process stays alive so operators can inspect /v1/health.
    """

    def __init__(self):
        self._dimensions: Dict[str, DimensionSpec] = {}
        self._startup_error: Optional[str] = None
        self._load()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self):
        """Load from DB.  Sets _startup_error on failure (no exception raised)."""
        try:
            from core.database import query_evaluation_dimensions, GS_DB_AVAILABLE
            if not GS_DB_AVAILABLE:
                self._startup_error = (
                    "PAP database is not reachable. "
                    "Set PAP_DB_PATH to the PAP SQLite file and run pap/bootstrap.py."
                )
                logger.critical(f"[EvaluationRegistry] {self._startup_error}")
                return

            rows = query_evaluation_dimensions()
            if not rows:
                self._startup_error = (
                    "PAP database is reachable but config_evaluation_dimensions is empty. "
                    "Run pap/bootstrap.py to seed the database."
                )
                logger.critical(f"[EvaluationRegistry] {self._startup_error}")
                return

            new_dims: Dict[str, DimensionSpec] = {}
            for row in rows:
                dim_id = row["dimension_id"]
                spec = {
                    "provider":         row["provider"],
                    "status":           row["status"],
                    "supported_roles":  row["supported_roles"],
                    "requires_context": row["requires_context"],
                }
                new_dims[dim_id] = DimensionSpec(dim_id, row["category"], spec)

            self._dimensions = new_dims
            self._startup_error = None          # clear any previous error
            logger.info(
                f"[EvaluationRegistry] Loaded from PAP DB: "
                f"{len(self.active_dimensions())} active, "
                f"{len(self.deferred_dimensions())} deferred"
            )

        except Exception as exc:
            self._startup_error = (
                f"DB load failed: {exc}. "
                "Check PAP_DB_PATH and run pap/bootstrap.py."
            )
            logger.critical(f"[EvaluationRegistry] {self._startup_error}", exc_info=True)

    def reload(self):
        """Hot-reload from DB.  Updates _startup_error / _dimensions in place."""
        self._load()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """True iff the taxonomy was loaded successfully."""
        return self._startup_error is None and bool(self._dimensions)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def all_dimensions(self) -> List[str]:
        return list(self._dimensions.keys())

    def active_dimensions(self) -> List[str]:
        return [n for n, d in self._dimensions.items() if d.status == "active"]

    def deferred_dimensions(self) -> List[str]:
        return [n for n, d in self._dimensions.items() if d.status == "deferred"]

    def deferred_set(self) -> Set[str]:
        return {n for n, d in self._dimensions.items() if d.status == "deferred"}

    def get_spec(self, dimension: str) -> Optional[DimensionSpec]:
        return self._dimensions.get(dimension)

    def is_deferred(self, dimension: str) -> bool:
        spec = self._dimensions.get(dimension)
        return spec is not None and spec.status == "deferred"

    def is_applicable_to_role(self, dimension: str, role: str) -> bool:
        spec = self._dimensions.get(dimension)
        return spec is not None and role in spec.roles

    def get_provider(self, dimension: str) -> Optional[str]:
        spec = self._dimensions.get(dimension)
        return spec.provider if spec else None

    def resolve_providers(
        self,
        dimensions: Optional[List[str]],
        role: str,
        include_deferred: bool = False,
    ) -> Dict[str, List[str]]:
        """
        Return a mapping of  provider_name -> [dimension, ...] for a request.

        Args:
            dimensions:       Explicit list requested, or None for all applicable.
            role:             ContentRole value string.
            include_deferred: If False (default), deferred dims are excluded.

        Returns:
            { "azure_content_safety": ["hate", "violence", ...], ... }
        """
        if dimensions is None:
            # All active (or all including deferred) dims applicable to role
            candidates = [
                n for n, d in self._dimensions.items()
                if (d.status == "active" or include_deferred)
                and role in d.roles
            ]
        else:
            candidates = [
                d for d in dimensions
                if d in self._dimensions
                and (self._dimensions[d].status == "active" or include_deferred)
                and role in self._dimensions[d].roles
            ]

        mapping: Dict[str, List[str]] = {}
        for dim in candidates:
            provider = self._dimensions[dim].provider
            if provider:
                mapping.setdefault(provider, []).append(dim)

        return mapping

    def as_catalog(self) -> List[dict]:
        """Serialisable list of all dimensions — used by GET /v1/evaluations."""
        return [
            {
                "dimension": d.name,
                "category":  d.category,
                "status":    d.status,
                "roles":     sorted(d.roles),
                "provider":  d.provider,
                "requires_context": d.requires_context,
            }
            for d in self._dimensions.values()
        ]
