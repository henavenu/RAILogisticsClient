"""
Pack Loader — loads threshold profiles and policy packs into structured
dataclasses used by the Context Classifier, Threshold Resolver, and
Chain Executor.

Load path:
  Reads config_threshold_profiles + config_service_configs('pe','threshold_profiles')
  for profiles, and config_policy_packs + config_pack_dimensions +
  config_service_configs('pe','policy_packs') for packs.

  AUDIT_SQLITE_PATH must be set. The DB is the sole source of truth —
  there is no YAML fallback.

All parsing and validation happens at startup. Runtime evaluation only reads
from pre-built dataclass objects — no DB queries at request time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Threshold primitives ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThresholdBand:
    """
    Resolved effective threshold band for one dimension at a given step.
    All three bands are contiguous and cover 1–9.
    """
    permissive: Tuple[int, int]
    normative:  Tuple[int, int]
    coercive:   Tuple[int, int]

    def verdict_for(self, score: int) -> str:
        """Return PERMISSIVE | NORMATIVE | COERCIVE for a raw GS score."""
        lo, hi = self.coercive
        if lo <= score <= hi:
            return "COERCIVE"
        lo, hi = self.normative
        if lo <= score <= hi:
            return "NORMATIVE"
        return "PERMISSIVE"


@dataclass
class ThresholdProfile:
    """
    Named threshold profile with step-map for sensitivity adjustment.
    max_step / min_step bound what the Threshold Resolver may apply.
    """
    id:       str
    base:     ThresholdBand
    max_step: int
    min_step: int
    step_map: Dict[int, ThresholdBand]  # step int → ThresholdBand

    def resolve(self, step: int) -> ThresholdBand:
        """
        Return the effective ThresholdBand for the requested step.
        Step is clamped to [min_step, max_step] before lookup.
        Falls back to base band if step not in map.
        """
        clamped = max(self.min_step, min(self.max_step, step))
        return self.step_map.get(clamped, self.base)


# ── Pack primitives ───────────────────────────────────────────────────────────

@dataclass
class PackDimension:
    """One dimension entry within a pack definition."""
    id:       str
    profile:  str           # profile name — looked up in ThresholdProfile registry
    severity: int           # 1 | 2 | 3
    abort:    bool          # abort chain on COERCIVE even in collect-all pack
    roles:    List[str]     # content roles this dim applies to; [] = all roles
    note:     str = ""


@dataclass
class ContextAdjustment:
    """
    Trust-signal-driven threshold step adjustment declared within a pack.
    When the condition fires, `step` is added to the Threshold Resolver's
    running step total for the named dimensions.
    Multiple adjustments can fire simultaneously — they are cumulative,
    bounded by the profile's max_step / min_step.
    """
    signal:   str           # trust signal name: session_trust_score | app_trust_score
    operator: str           # lt | lte | gt | gte | eq
    value:    float         # threshold value for the condition
    step:     int           # step delta to apply (positive = tighten)
    affects:  List[str]     # dimension ids affected; [] = all dims in this pack


@dataclass
class Pack:
    """Fully loaded pack definition."""
    id:                  str
    layer:               str      # mandatory | domain-mandatory | domain-addon | ...
    enforcement:         str      # always-on | flexible | flexible-threshold-only
    mode:                str      # abort-on-first-coercive | collect-all
    hitl_trigger:        bool
    version:             str
    owner:               str
    description:         str
    combining_algorithm: str = "worst_case"
    active:              bool = True   # False = pack loaded but skipped at evaluation time
    dimensions:          List[PackDimension] = field(default_factory=list)
    context_adjustments: List[ContextAdjustment] = field(default_factory=list)

    def dims_for_role(self, role: str) -> List[PackDimension]:
        """Return dimensions applicable to the given content role."""
        return [
            d for d in self.dimensions
            if not d.roles or role in d.roles
        ]

    @property
    def is_mandatory(self) -> bool:
        return self.layer == "mandatory"


# ── Loader ────────────────────────────────────────────────────────────────────

class PackLoader:
    """
    Loads threshold profiles and policy packs at startup.
    DB is the sole source of truth — AUDIT_SQLITE_PATH must be set.
    Provides dict-like access to profiles and packs by id.
    """

    def __init__(self):
        self._profiles: Dict[str, ThresholdProfile] = {}
        self._packs:    Dict[str, Pack]              = {}

        if not self._load_from_db():
            raise RuntimeError(
                "[PackLoader] Failed to load profiles/packs from DB. "
                "Ensure AUDIT_SQLITE_PATH is set and config_threshold_profiles / "
                "config_policy_packs tables are populated."
            )

        logger.info(
            f"[PackLoader] ready — "
            f"{len(self._profiles)} profiles, {len(self._packs)} packs "
            f"({', '.join(self._packs)})"
        )

    # ── DB load path (preferred) ──────────────────────────────────────────────

    def _load_from_db(self) -> bool:
        """
        Load profiles and packs from the shared PAP SQLite database.
        Returns True on success, False if DB unavailable or tables empty.
        """
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False

            from sqlalchemy import text

            with engine.connect() as conn:
                # ── Threshold profiles (base bands) ──────────────────────────
                prof_rows = conn.execute(
                    text(
                        "SELECT profile_id, "
                        "       permissive_lo, permissive_hi, "
                        "       normative_lo,  normative_hi, "
                        "       coercive_lo,   coercive_hi "
                        "FROM config_threshold_profiles"
                    )
                ).fetchall()

                # ── ServiceConfig blob: full threshold_profiles.yaml ──────────
                # Contains max_step, min_step, step_map not stored in the table.
                tp_blob_row = conn.execute(
                    text(
                        "SELECT config_json FROM config_service_configs "
                        "WHERE service_name = 'pe' AND config_key = 'threshold_profiles'"
                    )
                ).fetchone()

                # ── Policy packs ──────────────────────────────────────────────
                pack_rows = conn.execute(
                    text(
                        "SELECT pack_id, layer, enforcement, mode, hitl_trigger, "
                        "       version, owner, description, combining_algorithm, active "
                        "FROM config_policy_packs"
                    )
                ).fetchall()

                # ── Pack dimensions ───────────────────────────────────────────
                dim_rows = conn.execute(
                    text(
                        "SELECT pack_id, dimension_id, profile_id, severity, abort, "
                        "       active_roles, note "
                        "FROM config_pack_dimensions "
                        "ORDER BY pack_id, dimension_id"
                    )
                ).fetchall()

                # ── ServiceConfig blob: full policy_packs.yaml ────────────────
                # Contains context_adjustments not stored in config_pack_dimensions.
                pp_blob_row = conn.execute(
                    text(
                        "SELECT config_json FROM config_service_configs "
                        "WHERE service_name = 'pe' AND config_key = 'policy_packs'"
                    )
                ).fetchone()

            if not prof_rows and not pack_rows:
                logger.warning("[PackLoader] DB has no profiles or packs — falling back to YAML")
                return False

            # ── Parse threshold profiles ──────────────────────────────────────
            tp_blob: dict = {}
            if tp_blob_row:
                raw = tp_blob_row[0]
                tp_blob = raw if isinstance(raw, dict) else json.loads(raw)

            new_profiles: Dict[str, ThresholdProfile] = {}
            for row in prof_rows:
                pid = row[0]
                base = ThresholdBand(
                    permissive=(int(row[1]), int(row[2])),
                    normative=(int(row[3]), int(row[4])),
                    coercive=(int(row[5]), int(row[6])),
                )
                profile_cfg = (tp_blob.get("profiles") or {}).get(pid, {})
                step_map: Dict[int, ThresholdBand] = {}
                for key, band_cfg in (profile_cfg.get("step_map") or {}).items():
                    step_int = int(str(key).replace("+", ""))
                    try:
                        step_map[step_int] = self._parse_band(band_cfg)
                    except Exception as exc:
                        logger.warning(f"[PackLoader] profile '{pid}' step {key}: {exc}")
                new_profiles[pid] = ThresholdProfile(
                    id=pid,
                    base=base,
                    max_step=int(profile_cfg.get("max_step", 0)),
                    min_step=int(profile_cfg.get("min_step", 0)),
                    step_map=step_map,
                )

            # ── Parse packs ───────────────────────────────────────────────────
            # Group dimensions by pack_id for O(1) lookup
            dims_by_pack: Dict[str, List] = {}
            for dr in dim_rows:
                dims_by_pack.setdefault(dr[0], []).append(dr)

            # context_adjustments come from the policy_packs blob
            pp_blob: dict = {}
            if pp_blob_row:
                raw = pp_blob_row[0]
                pp_blob = raw if isinstance(raw, dict) else json.loads(raw)

            new_packs: Dict[str, Pack] = {}
            for row in pack_rows:
                pack_id = row[0]
                try:
                    # Build PackDimension list
                    dimensions: List[PackDimension] = []
                    for dr in dims_by_pack.get(pack_id, []):
                        roles_raw = dr[5]
                        if isinstance(roles_raw, str):
                            try:
                                roles = json.loads(roles_raw)
                            except Exception:
                                roles = []
                        else:
                            roles = list(roles_raw or [])
                        dimensions.append(PackDimension(
                            id=dr[1],
                            profile=dr[2],
                            severity=int(dr[3]),
                            abort=bool(dr[4]),
                            roles=roles,
                            note=str(dr[6] or ""),
                        ))

                    # Build ContextAdjustment list from blob
                    adjustments: List[ContextAdjustment] = []
                    pack_blob_cfg = (pp_blob.get("packs") or {}).get(pack_id, {})
                    raw_adj = pack_blob_cfg.get("context_adjustments") or []
                    if isinstance(raw_adj, list):
                        for adj in raw_adj:
                            if not isinstance(adj, dict):
                                continue
                            when  = adj.get("when") or {}
                            apply = adj.get("apply") or {}
                            adjustments.append(ContextAdjustment(
                                signal=when.get("signal", ""),
                                operator=when.get("operator", "lt"),
                                value=float(when.get("value", 0)),
                                step=int(apply.get("step", 0)),
                                affects=list(apply.get("affects") or []),
                            ))

                    new_packs[pack_id] = Pack(
                        id=pack_id,
                        layer=str(row[1] or "domain-addon"),
                        enforcement=str(row[2] or "flexible"),
                        mode=str(row[3] or "collect-all"),
                        hitl_trigger=bool(row[4]),
                        version=str(row[5] or "0.0.0"),
                        owner=str(row[6] or "unknown"),
                        description=str(row[7] or ""),
                        combining_algorithm=str(row[8] or "worst_case"),
                        active=bool(row[9]),
                        dimensions=dimensions,
                        context_adjustments=adjustments,
                    )
                except Exception as exc:
                    logger.error(f"[PackLoader] Failed to parse pack '{pack_id}' from DB: {exc}")

            self._profiles = new_profiles
            self._packs    = new_packs
            logger.info(
                f"[PackLoader] Loaded {len(self._profiles)} profile(s) and "
                f"{len(self._packs)} pack(s) from PAP DB"
            )
            return True

        except Exception as exc:
            logger.error(f"[PackLoader] DB load failed: {exc}")
            return False

    @staticmethod
    def _parse_band(cfg: dict) -> ThresholdBand:
        def _t(key) -> Tuple[int, int]:
            v = cfg[key]
            return (int(v[0]), int(v[1]))
        return ThresholdBand(
            permissive=_t("permissive"),
            normative=_t("normative"),
            coercive=_t("coercive"),
        )

    # ── Reload ────────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Hot-reload from DB. Raises RuntimeError if DB is unavailable."""
        self._profiles = {}
        self._packs    = {}
        if not self._load_from_db():
            raise RuntimeError(
                "[PackLoader] Hot-reload failed — DB unavailable or tables empty."
            )
        logger.info(
            f"[PackLoader] reloaded — "
            f"{len(self._profiles)} profiles, {len(self._packs)} packs"
        )

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_pack(self, pack_id: str) -> Optional[Pack]:
        return self._packs.get(pack_id)

    def get_profile(self, profile_id: str) -> Optional[ThresholdProfile]:
        return self._profiles.get(profile_id)

    def mandatory_packs(self) -> List[Pack]:
        return [p for p in self._packs.values() if p.is_mandatory]

    def layer2_packs(self) -> List[Pack]:
        return [p for p in self._packs.values() if not p.is_mandatory]
