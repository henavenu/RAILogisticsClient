"""
Drift Detector — pure Python, deterministic, no LLM involved.

Compares current T1 snapshot values against static thresholds from
config/thresholds.yaml. Upserts drift signals into ta_drift_signals.

Separation of concerns:
  - Detection (is it below threshold?) -> this file
  - Trend analysis (why? for how long?) -> LLM prompt context
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_THRESHOLDS: Optional[dict] = None
_CFG_PATH = Path(__file__).parent.parent / "config" / "thresholds.yaml"

_EXCLUDE_CATS = {"corporate_governance", "prompt_filter", "session_trackers"}


def _load_thresholds() -> dict:
    global _THRESHOLDS
    if _THRESHOLDS is None:
        with open(_CFG_PATH) as f:
            _THRESHOLDS = yaml.safe_load(f)
    return _THRESHOLDS


# ── T1 extraction helpers ─────────────────────────────────────────────────────

def aggregate_pillars(t1: dict) -> dict[str, dict]:
    """
    Aggregate T1 dim_scores (list format) into per-pillar summary.

    Returns:
        {pillar_name: {"comp_rate": float, "total": int, "flagged": int}}
    """
    dim_scores = t1.get("dim_scores", [])
    agg: dict[str, dict] = {}

    # Support legacy dict format (keyed by pillar) just in case
    if isinstance(dim_scores, dict):
        for pillar, data in dim_scores.items():
            if pillar in _EXCLUDE_CATS:
                continue
            viol = data.get("violation_rate_pct", 0)
            total = data.get("sample", 0)
            flagged = round(viol / 100 * total)
            agg[pillar] = {
                "comp_rate": round(1.0 - viol / 100.0, 4),
                "total":     total,
                "flagged":   flagged,
            }
        return agg

    # Normal list format
    for d in dim_scores:
        cat = d.get("category", "other")
        if cat in _EXCLUDE_CATS:
            continue
        if cat not in agg:
            agg[cat] = {"total": 0, "flagged": 0}
        agg[cat]["total"]   += d.get("sample", 0)
        agg[cat]["flagged"] += d.get("flagged_count", 0)

    for pillar, data in agg.items():
        total = data["total"]
        data["comp_rate"] = round(1.0 - data["flagged"] / total, 4) if total else None

    return agg


def pillar_compliance(t1: dict, pillar: str) -> Optional[float]:
    """Extract compliance rate (0.0-1.0) for a pillar from a T1 snapshot."""
    agg = aggregate_pillars(t1)
    data = agg.get(pillar)
    if not data or data.get("total", 0) == 0:
        return None
    return data.get("comp_rate")


def platform_trust_score(t1: dict) -> Optional[float]:
    """Extract platform-level trust score from T1 snapshot."""
    # Trust score lives in sessions, not T1 — fall back to violation-rate proxy
    metrics = t1.get("metrics", {})
    vr = metrics.get("violation_rate", {})
    flag_pct = vr.get("flag_rate_pct")
    if flag_pct is not None:
        return round(1.0 - float(flag_pct) / 100.0, 4)
    return None


# ── Historical snapshot loader ────────────────────────────────────────────────

def get_recent_snapshots(con: sqlite3.Connection, agent_type: str, n: int) -> list[dict]:
    """Load the last N snapshots, oldest first, for historical context."""
    rows = con.execute(
        """SELECT run_id, t1_json, t2_json, session_json, captured_at
           FROM ta_metric_snapshots
           WHERE agent_type = ?
           ORDER BY captured_at DESC
           LIMIT ?""",
        (agent_type, n),
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "run_id":      row["run_id"],
            "captured_at": row["captured_at"],
            "t1":          json.loads(row["t1_json"]      or "{}"),
            "t2":          json.loads(row["t2_json"]      or "{}"),
            "session":     json.loads(row["session_json"] or "{}"),
        })
    return list(reversed(result))  # oldest first for prompt readability


# ── Signal persistence ────────────────────────────────────────────────────────

def _upsert_signal(
    con: sqlite3.Connection,
    app_id: str,
    metric_path: str,
    signal_type: str,
    direction: str,
    current_value: float,
    threshold_green: float,
    threshold_amber: float,
    now: str,
) -> None:
    existing = con.execute(
        """SELECT signal_id FROM ta_drift_signals
           WHERE app_id=? AND metric_path=? AND resolved_at IS NULL""",
        (app_id, metric_path),
    ).fetchone()

    if existing:
        con.execute(
            """UPDATE ta_drift_signals
               SET last_seen_at=?, current_value=?,
                   consecutive_runs = consecutive_runs + 1
               WHERE signal_id=?""",
            (now, current_value, existing["signal_id"]),
        )
    else:
        con.execute(
            """INSERT INTO ta_drift_signals
               (signal_id, app_id, metric_path, signal_type, direction,
                first_seen_at, last_seen_at, threshold_green, threshold_amber,
                current_value, consecutive_runs)
               VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
            (
                str(uuid.uuid4()), app_id, metric_path, signal_type, direction,
                now, now, threshold_green, threshold_amber, current_value,
            ),
        )
    con.commit()


def _resolve_signal(con: sqlite3.Connection, app_id: str, metric_path: str, now: str) -> None:
    con.execute(
        """UPDATE ta_drift_signals
           SET resolved_at=?
           WHERE app_id=? AND metric_path=? AND resolved_at IS NULL""",
        (now, app_id, metric_path),
    )
    con.commit()


def load_active_signals(con: sqlite3.Connection, app_id: str = "all") -> list[dict]:
    rows = con.execute(
        """SELECT * FROM ta_drift_signals
           WHERE app_id=? AND resolved_at IS NULL
           ORDER BY consecutive_runs DESC""",
        (app_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Main entry point ──────────────────────────────────────────────────────────

def detect(con: sqlite3.Connection, agent_type: str, current_t1: dict) -> list[dict]:
    """
    Compare current T1 snapshot against static thresholds.
    Upserts/resolves drift signals. Returns active signals after update.
    """
    if not current_t1:
        return load_active_signals(con)

    thresholds = _load_thresholds()
    now = datetime.now(timezone.utc).isoformat()
    app_id = "all"

    pillar_agg = aggregate_pillars(current_t1)

    for pillar, thr in thresholds.get("pillars", {}).items():
        data = pillar_agg.get(pillar)
        if not data or data.get("total", 0) == 0 or data.get("comp_rate") is None:
            continue

        value  = data["comp_rate"]
        green  = float(thr["green"])
        amber  = float(thr["amber"])

        if value < amber:
            _upsert_signal(
                con, app_id,
                metric_path=f"t1.{pillar}",
                signal_type="compliance_drift",
                direction="degrading",
                current_value=value,
                threshold_green=green,
                threshold_amber=amber,
                now=now,
            )
        elif value < green:
            _upsert_signal(
                con, app_id,
                metric_path=f"t1.{pillar}",
                signal_type="compliance_watch",
                direction="degrading",
                current_value=value,
                threshold_green=green,
                threshold_amber=amber,
                now=now,
            )
        else:
            _resolve_signal(con, app_id, f"t1.{pillar}", now)

    return load_active_signals(con)
