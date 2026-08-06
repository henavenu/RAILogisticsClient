"""
App / Agent Trust Tracker.

Monitors session-level trust trajectory for each app and agent.
Detects trust score degradation, coercive verdict patterns, and
anomalous session behaviour (jailbreak + injection co-occurrence).

Prompt strategy for small local LLMs (phi3:mini etc.):
  - Python pre-formats session table with flagged rows highlighted
  - LLM gets concrete numbers — no arithmetic required
  - Short, imperative instructions; JSON template shown verbatim
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from core import collector, drift_detector, llm_client
from core.database import get_connection

logger = logging.getLogger(__name__)

_DB_PATH      = os.getenv("TA_DB_PATH", "./ta.db")
_WINDOW_MIN   = int(os.getenv("SNAPSHOT_INTERVAL_MINUTES", "2")) * 3
_CONTEXT_RUNS = 4


# ── Data formatting ───────────────────────────────────────────────────────────

def _fmt_t1_overview(t1: dict) -> str:
    """Extract platform-level metrics from T1 snapshot."""
    if not t1:
        return "  No T1 data available."

    lines = []
    total = t1.get("computed_from", 0)
    if total:
        lines.append(f"  Total evaluations:   {total}")

    metrics = t1.get("metrics", {})

    vr = metrics.get("violation_rate", {})
    if vr.get("total"):
        lines.append(
            f"  Overall violation:   {vr.get('flag_rate_pct', 0):.1f}%"
            f"  ({vr.get('flagged', 0)} COERCIVE out of {vr.get('total', 0)})"
        )

    pss = metrics.get("prompt_safety_score", {})
    if pss.get("avg") is not None:
        lines.append(f"  Prompt safety score: {pss['avg']:.3f}  (1.0=safe  sample={pss.get('sample', 0)})")

    rts = metrics.get("response_toxicity_score", {})
    if rts.get("avg") is not None:
        lines.append(f"  Response toxicity:   {rts['avg']:.3f}  (0.0=clean sample={rts.get('sample', 0)})")

    # Pillar-level compliance from dim_scores list
    pillar_agg = drift_detector.aggregate_pillars(t1)
    if pillar_agg:
        lines.append("  Pillar compliance:")
        for pillar in ["safety", "security", "fairness", "transparency", "compliance", "privacy"]:
            d = pillar_agg.get(pillar)
            if d and d.get("total", 0) > 0 and d.get("comp_rate") is not None:
                comp = round(d["comp_rate"] * 100, 1)
                flag = d["flagged"]
                tot  = d["total"]
                lines.append(f"    {pillar:<14} {comp:>5.1f}%  ({flag} flagged / {tot})")

    return "\n".join(lines) if lines else "  No trust metrics in this snapshot."


def _fmt_sessions(session: dict) -> tuple[str, list[dict]]:
    """Format session summaries; return (table_str, flagged_sessions_list)."""
    sessions = session.get("sessions", []) if isinstance(session, dict) else []
    if not sessions:
        return "  No session data for this window.", []

    lines = []
    flagged = []
    for s in sessions[:15]:
        sid    = str(s.get("session_id", "?"))[:12]
        app    = s.get("app_id", "?")
        turns  = s.get("turn_count", 0)
        coerc  = s.get("coercive_count", 0)
        trust  = s.get("final_trust_score", 1.0)
        reason = s.get("end_reason", "?")
        flag   = " <-- HIGH RISK" if coerc >= 2 or trust < 0.6 else ""
        lines.append(
            f"  session={sid}  app={app}  turns={turns}"
            f"  coercive={coerc}  trust={trust:.3f}  end={reason}{flag}"
        )
        if coerc >= 2 or trust < 0.6:
            flagged.append({
                "session_id":       s.get("session_id", "?"),
                "app_id":           app,
                "coercive_count":   coerc,
                "final_trust_score": trust,
                "risk":             "high" if coerc >= 3 or trust < 0.5 else "medium",
                "pattern":          f"{coerc} coercive verdicts, trust dropped to {trust:.2f}",
            })

    return "\n".join(lines), flagged


def _fmt_history_compact(snapshots: list[dict]) -> str:
    """One-line-per-run compact trust history."""
    if not snapshots:
        return "  None — this is the first run."
    lines = []
    for i, snap in enumerate(snapshots):
        ts      = snap.get("captured_at", "")[:16].replace("T", " ")
        t1      = snap.get("t1", {})
        metrics = t1.get("metrics", {})
        vr      = metrics.get("violation_rate", {})
        viol    = vr.get("flag_rate_pct")
        viol_s  = f"violation={viol:.1f}%" if viol is not None else "violation=n/a"
        total   = t1.get("computed_from", 0)
        lines.append(f"  Run-{len(snapshots) - i}  ({ts}):  {viol_s}  evals={total}")
    return "\n".join(lines)


def _fmt_signals(signals: list[dict]) -> str:
    if not signals:
        return "  None — trust metrics within threshold."
    lines = []
    for s in signals:
        comp = round(float(s.get("current_value", 0)) * 100, 1)
        runs = s.get("consecutive_runs", 1)
        lines.append(
            f"  {s['metric_path']}: {s['direction']}"
            f"  current={comp}%  consecutive_runs={runs}"
        )
    return "\n".join(lines)


# ── Prompt ────────────────────────────────────────────────────────────────────

_PROMPT = """\
You are an AI trust analyst monitoring a live AI application for session-level risk.

TRUST THRESHOLDS:
  >= 0.80 -> GREEN (healthy)   0.60-0.79 -> AMBER (watch)   < 0.60 -> RED (escalate)
  coercive_count >= 2 in a session -> elevated risk

CURRENT T1 SNAPSHOT  (last {window_min} min):
{current_t1}

SESSION SUMMARIES  (sessions ended in last {window_min} min):
{session_table}

HISTORY  ({n_hist} runs, oldest first):
{history}

DRIFT SIGNALS  (automated monitor):
{drift_signals}

---
Write a concise trust report. Be specific — use the numbers above. No disclaimers.

1. Is the violation rate trending up, flat, or down across the history runs?
2. For each HIGH RISK session (marked above): describe the violation pattern and risk.
3. What is the most likely root cause of any elevated risk?
4. State one specific action to mitigate the top risk.

After the narrative, output the structured finding on a new line starting with JSON_FINDING:
Do NOT wrap JSON in markdown code fences.

JSON_FINDING:
{{"overall_status": "{overall_status}", "severity": "{severity}", "summary": "<2-sentence summary>", "trust_score_current": {trust_current}, "trust_score_trend": "<improving|stable|degrading|insufficient_data>", "flagged_sessions": {flagged_json}, "findings": [{{"category": "<trust_drift|session_anomaly|coercive_pattern>", "observation": "<one sentence>", "recommended_action": "<one sentence>"}}]}}
"""


def _build_prompt(t1: dict, session: dict, history: list[dict], signals: list[dict]) -> tuple[str, str]:
    t1_text, (session_text, flagged) = _fmt_t1_overview(t1), _fmt_sessions(session)

    # Compute overall status from signals + session data
    has_breach  = any(s.get("signal_type") == "compliance_drift" for s in signals)
    has_watch   = any(s.get("signal_type") == "compliance_watch" for s in signals)
    has_coercive = len(flagged) > 0

    if has_breach or (has_coercive and len(flagged) >= 2):
        overall  = "degrading"
        severity = "high" if len(flagged) >= 3 else "medium"
    elif has_watch or has_coercive:
        overall  = "watch"
        severity = "low"
    else:
        overall  = "stable"
        severity = "none"

    # Current trust proxy: 1 - violation_rate
    metrics    = t1.get("metrics", {}) if t1 else {}
    vr         = metrics.get("violation_rate", {})
    trust_cur  = round(1.0 - float(vr.get("flag_rate_pct", 0)) / 100, 3) if vr.get("total") else "null"

    prompt = _PROMPT.format(
        window_min    = _WINDOW_MIN,
        current_t1    = t1_text,
        session_table = session_text,
        n_hist        = len(history),
        history       = _fmt_history_compact(history),
        drift_signals = _fmt_signals(signals),
        overall_status = overall,
        severity       = severity,
        trust_current  = trust_cur,
        flagged_json   = json.dumps(flagged[:5]),
    )
    return prompt, overall


# ── LLM response parsing ──────────────────────────────────────────────────────

def _parse_response(text: str) -> tuple[str, dict]:
    marker = "JSON_FINDING:"
    if marker in text:
        reasoning, _, raw_json = text.partition(marker)
        raw = raw_json.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            return reasoning.strip(), json.loads(raw.strip())
        except json.JSONDecodeError:
            logger.warning("[TrackerAgent] Could not parse JSON finding from LLM output")
    return text.strip(), {}


def _fallback_finding(drift_signals: list[dict], overall: str = "stable") -> dict:
    return {
        "overall_status":      overall if overall in ("stable", "watch", "degrading") else (
            "watch" if drift_signals else "stable"
        ),
        "severity":            "low" if drift_signals else "none",
        "summary":             "LLM unavailable. Drift detection results shown without narrative.",
        "trust_score_current": None,
        "trust_score_trend":   "insufficient_data",
        "flagged_sessions":    [],
        "findings":            [],
    }


# ── Persistence ───────────────────────────────────────────────────────────────

def _store_snapshot(con, run_id: str, t1: dict, t2: dict, session: dict) -> None:
    con.execute(
        """INSERT OR IGNORE INTO ta_metric_snapshots
           (run_id, agent_type, app_id, captured_at, window_minutes,
            t1_json, t2_json, session_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            run_id, "tracker", "all",
            datetime.now(timezone.utc).isoformat(),
            _WINDOW_MIN,
            json.dumps(t1),
            json.dumps(t2),
            json.dumps(session),
        ),
    )
    con.commit()


def _store_finding(con, run_id: str, reasoning: str, finding: dict, context_runs: int) -> str:
    finding_id = str(uuid.uuid4())
    con.execute(
        """INSERT INTO ta_findings
           (finding_id, run_id, agent_type, app_id, generated_at,
            overall_status, severity, summary_text, reasoning_trace,
            findings_json, context_runs)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            finding_id, run_id, "tracker", "all",
            datetime.now(timezone.utc).isoformat(),
            finding.get("overall_status", "stable"),
            finding.get("severity", "none"),
            finding.get("summary", ""),
            reasoning,
            json.dumps(finding),
            context_runs,
        ),
    )
    con.commit()
    return finding_id


# ── Public agent interface ────────────────────────────────────────────────────

async def run_sync() -> dict:
    """Run the tracker agent synchronously (background scheduler)."""
    run_id = str(uuid.uuid4())
    con    = get_connection(_DB_PATH)
    try:
        t1, t2, session = await asyncio.gather(
            collector.fetch_t1(window_minutes=_WINDOW_MIN),
            collector.fetch_t2(),
            collector.fetch_sessions(window_hours=1),
        )
        _store_snapshot(con, run_id, t1, t2, session)

        signals = drift_detector.detect(con, "tracker", t1)
        history = drift_detector.get_recent_snapshots(con, "tracker", _CONTEXT_RUNS)

        prompt, overall = _build_prompt(t1, session, history, signals)

        try:
            response_text = await llm_client.complete(prompt)
            reasoning, finding = _parse_response(response_text)
            if not finding:
                finding = _fallback_finding(signals, overall)
        except Exception as exc:
            logger.error(f"[TrackerAgent] LLM call failed: {exc}")
            reasoning = f"LLM unavailable: {exc}"
            finding   = _fallback_finding(signals, overall)

        finding_id = _store_finding(con, run_id, reasoning, finding, len(history))
        logger.info(
            f"[TrackerAgent] Run complete ->"
            f" status={finding.get('overall_status')} severity={finding.get('severity')}"
        )
        return {
            "finding_id":      finding_id,
            "run_id":          run_id,
            "agent_type":      "tracker",
            "overall_status":  finding.get("overall_status", "stable"),
            "severity":        finding.get("severity", "none"),
            "summary":         finding.get("summary", ""),
            "reasoning_trace": reasoning,
            "findings":        finding.get("findings", []),
            "drift_signals":   signals,
        }
    finally:
        con.close()


async def run_stream(run_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Run the tracker agent with streaming LLM output (SSE demo)."""
    run_id = run_id or str(uuid.uuid4())
    con    = get_connection(_DB_PATH)

    t1, t2, session = await asyncio.gather(
        collector.fetch_t1(window_minutes=_WINDOW_MIN),
        collector.fetch_t2(),
        collector.fetch_sessions(window_hours=1),
    )
    _store_snapshot(con, run_id, t1, t2, session)

    signals = drift_detector.detect(con, "tracker", t1)
    history = drift_detector.get_recent_snapshots(con, "tracker", _CONTEXT_RUNS)

    prompt, overall = _build_prompt(t1, session, history, signals)

    full_text: list[str] = []

    async def _generate():
        try:
            async for token in llm_client.stream_complete(prompt):
                full_text.append(token)
                yield token
        except Exception as exc:
            fallback = (
                f"\n\n[LLM unavailable: {exc}]\n\nJSON_FINDING:\n"
                + json.dumps(_fallback_finding(signals, overall))
            )
            full_text.append(fallback)
            yield fallback
        finally:
            reasoning, finding = _parse_response("".join(full_text))
            if not finding:
                finding = _fallback_finding(signals, overall)
            _store_finding(con, run_id, reasoning, finding, len(history))
            con.close()

    return _generate()
