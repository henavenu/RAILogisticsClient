"""
EU AI Act Compliance Monitor.

Scheduled agent that:
  1. Collects T1/T2 snapshots from PAP
  2. Stores raw snapshot in ta_metric_snapshots
  3. Detects compliance drift against static thresholds (deterministic)
  4. Builds a concise prompt with pre-computed pillar status labels
  5. Calls LLM — either streaming (SSE demo) or sync (background scheduler)
  6. Parses reasoning trace + structured JSON finding
  7. Stores finding in ta_findings; updates ta_drift_signals

Prompt strategy for small local LLMs (phi3:mini etc.):
  - Python pre-computes GREEN/AMBER/RED for every pillar
  - LLM gets a ready-made status table — no arithmetic required
  - Short, imperative instructions; no multi-paragraph preambles
  - JSON template is compact and shown verbatim
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

_DB_PATH       = os.getenv("TA_DB_PATH", "./ta.db")
_WINDOW_MIN    = int(os.getenv("SNAPSHOT_INTERVAL_MINUTES", "2")) * 3
_CONTEXT_RUNS  = 4

# ── Pillar metadata ───────────────────────────────────────────────────────────

_PILLAR_ORDER = ["safety", "security", "privacy", "compliance", "fairness", "transparency"]

_PILLAR_META = {
    "safety":       {"green": 0.97, "amber": 0.90, "article": "Art.9  (risk management)"},
    "security":     {"green": 0.95, "amber": 0.85, "article": ""},
    "privacy":      {"green": 0.95, "amber": 0.85, "article": "GDPR Art.5 (data minimisation)"},
    "compliance":   {"green": 0.95, "amber": 0.85, "article": "Art.5  (prohibited practices)"},
    "fairness":     {"green": 0.98, "amber": 0.90, "article": "Art.10 (bias/discrimination)"},
    "transparency": {"green": 0.93, "amber": 0.80, "article": "Art.13 (AI disclosure)"},
}


# ── Data formatting ───────────────────────────────────────────────────────────

def _build_pillar_table(t1: dict) -> tuple[str, str, list[dict], int]:
    """
    Aggregate dim_scores by pillar, assign GREEN/AMBER/RED.

    Returns:
        (table_str, overall_status, concern_list, total_evals)
    """
    pillar_agg = drift_detector.aggregate_pillars(t1)
    if not pillar_agg:
        return "  No pillar data — no evaluations in this window.", "unknown", [], 0

    lines: list[str] = []
    concerns: list[dict] = []
    worst = "green"
    total_evals = sum(d.get("total", 0) for d in pillar_agg.values())

    for pillar in _PILLAR_ORDER:
        data = pillar_agg.get(pillar)
        if not data or data.get("total", 0) == 0 or data.get("comp_rate") is None:
            continue

        comp     = round(data["comp_rate"] * 100, 1)
        total    = data["total"]
        flagged  = data["flagged"]
        meta     = _PILLAR_META.get(pillar, {"green": 0.95, "amber": 0.85, "article": ""})
        green_pct = meta["green"] * 100
        amber_pct = meta["amber"] * 100

        if data["comp_rate"] >= meta["green"]:
            status = "GREEN"
            tag    = ""
        elif data["comp_rate"] >= meta["amber"]:
            status = "AMBER"
            tag    = f"  <- {round(green_pct - comp, 1)}pp below green"
            if worst == "green":
                worst = "amber"
            concerns.append({"pillar": pillar, "comp": comp, "status": "amber",
                             "article": meta["article"], "flagged": flagged, "total": total})
        else:
            status = "RED"
            tag    = f"  <- {round(green_pct - comp, 1)}pp below green, BREACH"
            worst  = "red"
            concerns.append({"pillar": pillar, "comp": comp, "status": "red",
                             "article": meta["article"], "flagged": flagged, "total": total})

        lines.append(
            f"  {pillar:<14} {comp:>5.1f}%  {status:<6}"
            f"  (thresh: >={green_pct:.0f}% green / >={amber_pct:.0f}% amber"
            f"  |  {flagged}/{total} evals flagged){tag}"
        )

    if not lines:
        return "  No pillar data — no evaluations in this window.", "unknown", [], 0

    overall = "breach" if worst == "red" else "watch" if worst == "amber" else "compliant"
    return "\n".join(lines), overall, concerns, total_evals


def _fmt_history_compact(snapshots: list[dict]) -> str:
    """Compact one-line-per-run historical summary."""
    if not snapshots:
        return "  None — this is the first run."
    lines = []
    for i, snap in enumerate(snapshots):
        ts  = snap.get("captured_at", "")[:16].replace("T", " ")
        t1  = snap.get("t1", {})
        agg = drift_detector.aggregate_pillars(t1)
        parts = []
        for pillar in _PILLAR_ORDER:
            d = agg.get(pillar)
            if d and d.get("total", 0) > 0 and d.get("comp_rate") is not None:
                comp = round(d["comp_rate"] * 100, 1)
                parts.append(f"{pillar}={comp}%")
        scores = "  ".join(parts) if parts else "no data"
        lines.append(f"  Run-{len(snapshots) - i}  ({ts}):  {scores}")
    return "\n".join(lines)


def _fmt_signals(signals: list[dict]) -> str:
    if not signals:
        return "  None — all pillars within threshold."
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
You are an EU AI Act compliance analyst reviewing real-time pillar compliance data.

PILLAR COMPLIANCE  (last {window_min} min  |  {total_evals} total evaluations):
{pillar_table}

HISTORY  ({n_hist} prior runs, oldest first):
{history}

DRIFT SIGNALS  (automated threshold monitor):
{drift_signals}

---
Write a concise compliance report. Be specific and factual — use the numbers above.
Do NOT add disclaimers, do NOT repeat the instructions back.

For each AMBER or RED pillar:
  - Name the EU AI Act article implicated.
  - State what the {comp_pct} compliance rate means operationally.
  - Give one specific corrective action (e.g. "tighten prompt_injection threshold in safety policy").

For GREEN pillars: one short sentence confirming compliance.

Note any trend visible across the history runs (improving / stable / degrading).

After the narrative, output the structured finding on a new line starting with JSON_FINDING:
Do NOT wrap the JSON in markdown code fences.

JSON_FINDING:
{{"overall_status": "{overall_status}", "severity": "{severity}", "summary": "<2-sentence executive summary>", "articles_implicated": {articles_json}, "findings": [{finding_template}]}}
"""

_FINDING_TEMPLATE = """\
{{"article": "<Art.5|Art.9|Art.10|Art.13|none>", "pillar": "<pillar>", "current_compliance_pct": <number>, "status": "<green|amber|red>", "trend": "<improving|stable|degrading|insufficient_data>", "observation": "<one sentence>", "recommended_action": "<one sentence>"}}\
"""


def _build_prompt(t1: dict, history: list[dict], signals: list[dict]) -> tuple[str, str]:
    table, overall, concerns, total_evals = _build_pillar_table(t1)
    severity = (
        "critical" if overall == "breach" and len(concerns) > 2
        else "high"   if overall == "breach"
        else "medium" if overall == "watch" and len(concerns) > 1
        else "low"    if overall == "watch"
        else "none"
    )
    articles = list({c["article"] for c in concerns if c["article"]})
    comp_example = f"{concerns[0]['comp']}" if concerns else "N/A"

    prompt = _PROMPT.format(
        window_min    = _WINDOW_MIN,
        total_evals   = total_evals,
        pillar_table  = table,
        n_hist        = len(history),
        history       = _fmt_history_compact(history),
        drift_signals = _fmt_signals(signals),
        overall_status = overall,
        severity       = severity,
        articles_json  = json.dumps(articles),
        finding_template = _FINDING_TEMPLATE,
        comp_pct       = comp_example + "%",
    )
    return prompt, overall


# ── LLM response parsing ──────────────────────────────────────────────────────

def _parse_response(text: str) -> tuple[str, dict]:
    """Split LLM output into (reasoning_trace, finding_dict)."""
    marker = "JSON_FINDING:"
    if marker in text:
        reasoning, _, raw_json = text.partition(marker)
        raw = raw_json.strip()
        # Strip markdown fences if the model added them despite instructions
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            return reasoning.strip(), json.loads(raw.strip())
        except json.JSONDecodeError:
            logger.warning("[ComplianceAgent] Could not parse JSON finding from LLM output")
    return text.strip(), {}


def _fallback_finding(drift_signals: list[dict], overall: str = "watch") -> dict:
    status = overall if overall in ("compliant", "watch", "breach") else (
        "watch" if drift_signals else "compliant"
    )
    return {
        "overall_status": status,
        "severity":       "low" if drift_signals else "none",
        "summary":        "LLM unavailable. Drift detection results shown without narrative synthesis.",
        "articles_implicated": [],
        "findings": [],
    }


# ── Persistence ───────────────────────────────────────────────────────────────

def _store_snapshot(con, run_id: str, t1: dict, t2: dict, session: dict) -> None:
    con.execute(
        """INSERT OR IGNORE INTO ta_metric_snapshots
           (run_id, agent_type, app_id, captured_at, window_minutes,
            t1_json, t2_json, session_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            run_id, "compliance", "all",
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
            finding_id, run_id, "compliance", "all",
            datetime.now(timezone.utc).isoformat(),
            finding.get("overall_status", "watch"),
            finding.get("severity", "low"),
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
    """Run the compliance agent synchronously (background scheduler)."""
    run_id = str(uuid.uuid4())
    con    = get_connection(_DB_PATH)
    try:
        t1, t2, session = await asyncio.gather(
            collector.fetch_t1(window_minutes=_WINDOW_MIN),
            collector.fetch_t2(),
            collector.fetch_sessions(window_hours=1),
        )
        _store_snapshot(con, run_id, t1, t2, session)

        signals = drift_detector.detect(con, "compliance", t1)
        history = drift_detector.get_recent_snapshots(con, "compliance", _CONTEXT_RUNS)

        prompt, overall = _build_prompt(t1, history, signals)

        try:
            response_text = await llm_client.complete(prompt)
            reasoning, finding = _parse_response(response_text)
            if not finding:
                finding = _fallback_finding(signals, overall)
        except Exception as exc:
            logger.error(f"[ComplianceAgent] LLM call failed: {exc}")
            reasoning = f"LLM unavailable: {exc}"
            finding   = _fallback_finding(signals, overall)

        finding_id = _store_finding(con, run_id, reasoning, finding, len(history))
        logger.info(
            f"[ComplianceAgent] Run complete ->"
            f" status={finding.get('overall_status')} severity={finding.get('severity')}"
        )
        return {
            "finding_id":      finding_id,
            "run_id":          run_id,
            "agent_type":      "compliance",
            "overall_status":  finding.get("overall_status", "watch"),
            "severity":        finding.get("severity", "low"),
            "summary":         finding.get("summary", ""),
            "reasoning_trace": reasoning,
            "findings":        finding.get("findings", []),
            "drift_signals":   signals,
        }
    finally:
        con.close()


async def run_stream(run_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Run the compliance agent with streaming LLM output (SSE demo)."""
    run_id = run_id or str(uuid.uuid4())
    con    = get_connection(_DB_PATH)

    t1, t2, session = await asyncio.gather(
        collector.fetch_t1(window_minutes=_WINDOW_MIN),
        collector.fetch_t2(),
        collector.fetch_sessions(window_hours=1),
    )
    _store_snapshot(con, run_id, t1, t2, session)

    signals = drift_detector.detect(con, "compliance", t1)
    history = drift_detector.get_recent_snapshots(con, "compliance", _CONTEXT_RUNS)

    prompt, overall = _build_prompt(t1, history, signals)

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
