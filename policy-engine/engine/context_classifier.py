"""
Context Classifier — maps raw request metadata to a set of named context tags.

Tags are the bridge between transaction metadata and policy pack activation.
The orchestration agent reads the tag set produced here — it does not compute
tags itself. This keeps pack selection fully deterministic and operator-defined.

Rules are stored in the DB under config_service_configs('pe', 'context_routing').
Each rule has one or more condition blocks (AND within a block, OR across blocks).
All matching rules fire — a request can carry multiple tags simultaneously.

DB is the sole source of truth — AUDIT_SQLITE_PATH must be set.

Supported operators:
  eq        exact match (string, bool, number)
  neq       not equal
  gt / gte  numeric greater-than / greater-than-or-equal
  lt / lte  numeric less-than / less-than-or-equal
  in        value is in a list field
  contains  list field contains a value
  exists    field is present and non-null (value: true) or absent/null (value: false)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


def _get_nested(obj: Any, path: str) -> Any:
    """
    Walk a dot-separated path through nested dicts.
    e.g. "metadata.transaction_context.amount" → obj["metadata"]["transaction_context"]["amount"]
    Returns None if any segment is missing.
    """
    parts = path.split(".")
    cur = obj
    for part in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _eval_condition(field_value: Any, operator: str, threshold: Any) -> bool:
    """Evaluate a single condition against a resolved field value."""
    op = operator.lower().strip()

    if op == "exists":
        is_present = field_value is not None
        return is_present if threshold else not is_present

    if field_value is None:
        # Field absent — only exists operator can match; all others fail cleanly
        return False

    if op == "eq":
        # Coerce booleans from YAML strings ("true" / "false")
        if isinstance(threshold, str) and isinstance(field_value, bool):
            return field_value == (threshold.lower() == "true")
        return field_value == threshold

    if op == "neq":
        return field_value != threshold

    if op == "gt":
        return float(field_value) > float(threshold)

    if op == "gte":
        return float(field_value) >= float(threshold)

    if op == "lt":
        return float(field_value) < float(threshold)

    if op == "lte":
        return float(field_value) <= float(threshold)

    if op == "in":
        # field_value is a scalar; threshold is a list
        if not isinstance(threshold, list):
            return False
        return field_value in threshold

    if op == "contains":
        # field_value is a list; threshold is a scalar
        if not isinstance(field_value, list):
            return False
        return threshold in field_value

    logger.warning(f"ContextClassifier: unknown operator '{operator}' — condition skipped")
    return False


class ContextClassifier:
    """
    Evaluates classifier rules against request metadata and returns the set of
    active context tags.

    Loaded once at startup. Thread-safe (read-only after init).
    DB is the sole source of truth — AUDIT_SQLITE_PATH must be set.
    """

    def __init__(self):
        self._rules: List[Dict] = []
        self._pack_activation: List[Dict] = []

        if not self._load_from_db():
            raise RuntimeError(
                "[ContextClassifier] Failed to load context_routing from DB. "
                "Ensure AUDIT_SQLITE_PATH is set and config_service_configs "
                "contains a pe/context_routing blob."
            )

        logger.info(
            f"[ContextClassifier] loaded — "
            f"{len(self._rules)} classifier rules, "
            f"{len(self._pack_activation)} pack activation entries"
        )

    # ── DB load path (preferred) ──────────────────────────────────────────────

    def _load_from_db(self) -> bool:
        """
        Load context routing rules from config_service_configs('pe','context_routing').
        Returns True on success, False if DB unavailable or blob absent.
        """
        try:
            from core.database import engine, DB_AVAILABLE
            if not DB_AVAILABLE:
                return False

            from sqlalchemy import text
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT config_json FROM config_service_configs "
                        "WHERE service_name = 'pe' AND config_key = 'context_routing'"
                    )
                ).fetchone()

            if not row:
                logger.warning(
                    "[ContextClassifier] No context_routing blob in DB — falling back to YAML"
                )
                return False

            raw = row[0]
            if isinstance(raw, str):
                raw = json.loads(raw)

            self._rules           = raw.get("classifier") or []
            self._pack_activation = raw.get("pack_activation") or []
            logger.info(
                f"[ContextClassifier] Loaded from PAP DB — "
                f"{len(self._rules)} rules, {len(self._pack_activation)} activations"
            )
            return True

        except Exception as exc:
            logger.error(f"[ContextClassifier] DB load failed: {exc}")
            return False

    # ── Hot-reload ────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Hot-reload from DB. Raises RuntimeError if DB is unavailable."""
        if not self._load_from_db():
            raise RuntimeError(
                "[ContextClassifier] Hot-reload failed — DB unavailable or blob missing."
            )
        logger.info(
            f"[ContextClassifier] reloaded — "
            f"{len(self._rules)} rules, {len(self._pack_activation)} activations"
        )

    # ── Classification ────────────────────────────────────────────────────────

    def classify(self, metadata: Dict) -> Set[str]:
        """
        Evaluate all classifier rules against the provided metadata dict.
        Returns the set of tag names whose rules fired.

        A tag fires if ANY of its rule blocks match
        (multiple condition blocks per rule = OR logic).
        All conditions within a single block must match (AND logic).
        """
        tags: Set[str] = set()

        for rule in self._rules:
            tag   = rule.get("tag", "")
            rules = rule.get("rules") or []

            for condition_block in rules:
                conditions = condition_block.get("conditions") or []
                if self._all_match(conditions, metadata):
                    tags.add(tag)
                    logger.debug(f"ContextClassifier: tag '{tag}' activated")
                    break   # OR logic — one block match is enough

        return tags

    def active_packs_for_tags(self, tags: Set[str]) -> List[str]:
        """
        Return the list of pack ids activated by the given tag set.
        A pack activates if ANY of its listed tags is present.
        """
        active: List[str] = []
        for entry in self._pack_activation:
            pack_id      = entry.get("pack", "")
            activates_on = entry.get("activates_on") or []
            if tags.intersection(activates_on):
                active.append(pack_id)
                logger.debug(
                    f"ContextClassifier: pack '{pack_id}' activated "
                    f"(matching tags: {tags.intersection(activates_on)})"
                )
        return active

    @staticmethod
    def _all_match(conditions: List[Dict], metadata: Dict) -> bool:
        """Return True only if every condition in the block matches (AND)."""
        for cond in conditions:
            source   = cond.get("source", "")
            operator = cond.get("operator", "eq")
            value    = cond.get("value")

            field_value = _get_nested(metadata, source)
            if not _eval_condition(field_value, operator, value):
                return False
        return True
