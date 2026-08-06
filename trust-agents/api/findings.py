"""
GET  /v1/trust/findings  — latest finding per agent type + enriched deterministic metadata
GET  /v1/trust/drift     — active drift signals
GET  /v1/trust/history   — last N findings (both agent types)
GET  /v1/trust/status    — agent health

Design principle: STATUS and METRICS always come from deterministic Python.
The LLM produces narrative only — key_findings bullets, reasoning_trace.
If LLM is offline the status/metrics are still correct.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from core.database import get_connection
from core.drift_detector import aggregate_pillars

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/trust", tags=["Trust Intelligence"])

_DB_PATH = os.getenv("TA_DB_PATH", "./ta.db")

# Pillar weights for weighted-average compliance score (higher = more important)
_WEIGHTS = {
    "safety":       2.0,
    "fairness":     1.5,
    "transparency": 1.5,
    "compliance":   1.0,
    "security":     1.0,
    "privacy":      1.0,
}
_GREEN_THRESH = {
    "safety": 0.97, "security": 0.95, "privacy": 0.95,
    "compliance": 0.95, "fairness": 0.98, "transparency": 0.93,
}
_PILLAR_ORDER = ["safety", "security", "privacy", "compliance", "fairness", "transparency"]


# ── Deterministic enrichment helpers ─────────────────────────────────────────

def _t1_summary(t1_json_str: Optional[str]) -> dict:
    """
    Compute real metrics from a stored T1 snapshot JSON string.
    No LLM involved — pure Python aggregation.
    Returns: has_data, compliance_rate, worst_pillar, violation_rate_pct
    """
    base = {"has_data": False, "compliance_rate": None,
            "worst_pillar": None, "violation_rate_pct": None}
    try:
        t1 = json.loads(t1_json_str or "{}")
    except (json.JSONDecodeError, TypeError):
        return base

    agg = aggregate_pillars(t1)
    if not agg:
        return base

    has_data = any(d.get("total", 0) > 0 for d in agg.values())
    if not has_data:
        return base

    # Weighted compliance rate across pillars
    w_sum, w_tot = 0.0, 0.0
    worst_name, worst_gap = None, 0.0

    for pillar in _PILLAR_ORDER:
        d = agg.get(pillar)
        if not d or d.get("total", 0) == 0 or d.get("comp_rate") is None:
            continue
        comp = d["comp_rate"]
        w = _WEIGHTS.get(pillar, 1.0)
        w_sum += comp * w
        w_tot += w
        gap = _GREEN_THRESH.get(pillar, 0.95) - comp
        if gap > worst_gap:
            worst_gap = gap
            worst_name = pillar

    comp_rate = round(w_sum / w_tot * 100, 1) if w_tot > 0 else None

    worst = None
    if worst_name and worst_gap > 0:
        d = agg[worst_name]
        worst = {
            "name":      worst_name,
            "rate":      round(d["comp_rate"] * 100, 1),
            "threshold": round(_GREEN_THRESH.get(worst_name, 0.95) * 100, 1),
            "gap_pp":    round(worst_gap * 100, 1),
        }

    # Violation rate from T1 metrics section
    metrics = t1.get("metrics", {})
    vr_raw  = metrics.get("violation_rate", {})
    viol_pct = (
        round(float(vr_raw["flag_rate_pct"]), 1)
        if vr_raw.get("total") and vr_raw.get("flag_rate_pct") is not None
        else None
    )

    return {
        "has_data":           True,
        "compliance_rate":    comp_rate,
        "worst_pillar":       worst,
        "violation_rate_pct": viol_pct,
    }


def _det_status(
    signals: list[dict],
    agent_type: str,
    has_data: bool,
) -> str:
    """
    Deterministic status from drift signals — never from LLM output.
    compliance agent: standby | compliant | watch | breach
    tracker agent:    standby | stable    | watch | degrading
    """
    if not has_data:
        return "standby"

    breach = [s for s in signals if s.get("signal_type") == "compliance_drift"]
    watch  = [s for s in signals if s.get("signal_type") == "compliance_watch"]

    if breach:
        return "breach" if agent_type == "compliance" else "degrading"
    if watch:
        return "watch"
    return "compliant" if agent_type == "compliance" else "stable"


def _key_findings(findings_json) -> list[dict]:
    """Extract top 3 LLM finding bullets (observation + recommended_action)."""
    if not findings_json:
        return []
    if isinstance(findings_json, str):
        try:
            findings_json = json.loads(findings_json)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(findings_json, dict):
        return []

    out = []
    for f in (findings_json.get("findings") or [])[:3]:
        obs = (f.get("observation") or f.get("pattern") or "").strip()
        rec = (f.get("recommended_action") or "").strip()
        if obs:
            out.append({"observation": obs, "recommended_action": rec})
    return out


def _llm_was_used(reasoning: Optional[str], summary: Optional[str]) -> bool:
    """True if the finding was produced by an LLM (not a fallback)."""
    combined = ((reasoning or "") + (summary or "")).lower()
    return combined != "" and "llm unavailable" not in combined


# ── Enrichment pipeline ───────────────────────────────────────────────────────

def _enrich(
    row: Optional[sqlite3.Row],
    snap_row: Optional[sqlite3.Row],
    signals: list[dict],
    agent_type: str,
) -> dict:
    """
    Merge a ta_findings row with its snapshot T1 data + active signals
    into a fully-enriched response dict.
    """
    if row is None:
        # No finding stored yet — return explicit standby shell
        return {
            "agent_type":           agent_type,
            "deterministic_status": "standby",
            "has_data":             False,
            "compliance_rate":      None,
            "worst_pillar":         None,
            "violation_rate_pct":   None,
            "llm_available":        False,
            "key_findings":         [],
            "overall_status":       None,
            "severity":             None,
            "summary_text":         None,
            "reasoning_trace":      None,
            "findings_json":        None,
            "generated_at":         None,
            "finding_id":           None,
        }

    d = dict(row)

    # Parse findings_json
    fj = d.get("findings_json")
    if isinstance(fj, str):
        try:
            fj = json.loads(fj)
        except (json.JSONDecodeError, TypeError):
            fj = None
    d["findings_json"] = fj

    # T1 summary from snapshot
    t1_str  = snap_row["t1_json"] if snap_row else None
    t1_meta = _t1_summary(t1_str)

    d.update({
        "agent_type":           agent_type,
        "has_data":             t1_meta["has_data"],
        "compliance_rate":      t1_meta["compliance_rate"],
        "worst_pillar":         t1_meta["worst_pillar"],
        "violation_rate_pct":   t1_meta["violation_rate_pct"],
        "deterministic_status": _det_status(signals, agent_type, t1_meta["has_data"]),
        "llm_available":        _llm_was_used(d.get("reasoning_trace"), d.get("summary_text")),
        "key_findings":         _key_findings(fj),
    })
    return d


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/findings")
async def get_findings():
    """
    Latest finding for each agent type, fully enriched with deterministic metadata.
    Status always reflects real evaluation data — never LLM output.
    """
    con = get_connection(_DB_PATH)
    try:
        def _latest(agent_type: str):
            row = con.execute(
                """SELECT f.*, s.t1_json
                   FROM ta_findings f
                   LEFT JOIN ta_metric_snapshots s
                     ON f.run_id = s.run_id AND s.agent_type = f.agent_type
                   WHERE f.agent_type = ?
                   ORDER BY f.generated_at DESC LIMIT 1""",
                (agent_type,),
            ).fetchone()
            snap = None
            if row and "t1_json" not in row.keys():
                # Fallback: grab latest snapshot directly
                snap = con.execute(
                    """SELECT t1_json FROM ta_metric_snapshots
                       WHERE agent_type=? ORDER BY captured_at DESC LIMIT 1""",
                    (agent_type,),
                ).fetchone()
            return row, snap

        drift_rows = con.execute(
            """SELECT * FROM ta_drift_signals
               WHERE resolved_at IS NULL
               ORDER BY consecutive_runs DESC""",
        ).fetchall()
        signals = [dict(r) for r in drift_rows]

        comp_row,  comp_snap  = _latest("compliance")
        track_row, track_snap = _latest("tracker")

        # Extract snapshot row (t1_json is joined into finding row)
        def _snap_from_row(r):
            if r and "t1_json" in (r.keys() if hasattr(r, "keys") else {}):
                return r
            return None

        return {
            "compliance": _enrich(comp_row,  _snap_from_row(comp_row),  signals, "compliance"),
            "tracker":    _enrich(track_row, _snap_from_row(track_row), signals, "tracker"),
            "drift":      signals,
        }
    finally:
        con.close()


@router.get("/drift")
async def get_drift():
    """All active drift signals (unresolved)."""
    con = get_connection(_DB_PATH)
    try:
        rows = con.execute(
            """SELECT * FROM ta_drift_signals
               WHERE resolved_at IS NULL
               ORDER BY consecutive_runs DESC, last_seen_at DESC""",
        ).fetchall()
        return {"signals": [dict(r) for r in rows], "count": len(rows)}
    finally:
        con.close()


@router.get("/history")
async def get_history(
    agent_type: str = Query("compliance", pattern="^(compliance|tracker)$"),
    limit:      int = Query(20, ge=1, le=100),
):
    """Last N findings for an agent type, newest first."""
    con = get_connection(_DB_PATH)
    try:
        rows = con.execute(
            """SELECT f.finding_id, f.agent_type, f.generated_at,
                      f.overall_status, f.severity, f.summary_text,
                      f.context_runs, s.captured_at as snapshot_at
               FROM ta_findings f
               LEFT JOIN ta_metric_snapshots s ON f.run_id = s.run_id
               WHERE f.agent_type = ?
               ORDER BY f.generated_at DESC
               LIMIT ?""",
            (agent_type, limit),
        ).fetchall()
        return {"findings": [dict(r) for r in rows], "count": len(rows)}
    finally:
        con.close()


@router.get("/status")
async def get_status():
    """Agent health — last run timestamps and deterministic status."""
    con = get_connection(_DB_PATH)
    try:
        def _agent_status(agent_type: str) -> dict:
            row = con.execute(
                """SELECT generated_at, overall_status, severity
                   FROM ta_findings WHERE agent_type=?
                   ORDER BY generated_at DESC LIMIT 1""",
                (agent_type,),
            ).fetchone()
            snap = con.execute(
                """SELECT captured_at, t1_json FROM ta_metric_snapshots
                   WHERE agent_type=? ORDER BY captured_at DESC LIMIT 1""",
                (agent_type,),
            ).fetchone()
            t1_meta = _t1_summary(snap["t1_json"] if snap else None)
            return {
                "last_finding_at":      row["generated_at"]   if row  else None,
                "last_snapshot_at":     snap["captured_at"]   if snap else None,
                "overall_status":       row["overall_status"] if row  else None,
                "severity":             row["severity"]        if row  else None,
                "has_data":             t1_meta["has_data"],
                "compliance_rate":      t1_meta["compliance_rate"],
                "violation_rate_pct":   t1_meta["violation_rate_pct"],
            }

        active_signals = con.execute(
            "SELECT COUNT(*) as n FROM ta_drift_signals WHERE resolved_at IS NULL"
        ).fetchone()["n"]

        return {
            "compliance":     _agent_status("compliance"),
            "tracker":        _agent_status("tracker"),
            "active_signals": active_signals,
        }
    finally:
        con.close()
