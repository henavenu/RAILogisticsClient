"""
Phase 2 — continuous emergent-risk detector loop (Sentinel).

Off the critical path. Each tick it:
  1. discovers active sessions via the PE audit pull (GET /v1/audit/events?app_id),
  2. reads each session's risk record (GET /internal/session-risk/{id}),
  3. for sessions the DETERMINISTIC layer flagged (WATCH/CONFIRMED) and not already
     frozen, runs an agentic detector on the session's payment stream,
  4. writes back:
        CONFIRMED -> POST /internal/session-freeze   (trip the circuit-breaker)
        CLEARED   -> POST /internal/session-verdict   (release the deterministic false positive)

This is the "deterministic over-flags -> agent refines (confirm or clear)" design, made
real and count-free, off the sync path. Reuses Sentinel's existing LLM provider chain.
"""
import asyncio
import json
import logging
import re

import httpx

import platform_activity as _pa

logger = logging.getLogger("sentinel.emergent")

_STRUCTURING_SYS = (
    "You are a financial-crime STRUCTURING detector inside a bank's governance platform. "
    "You receive the recent payment activity of ONE customer session. Structuring (smurfing) "
    "is deliberately splitting funds into multiple transactions to stay just under a reporting "
    "threshold (USD 10,000). Indicators: several payments individually just under the threshold, "
    "to the same or related payee, clustered closely in time. Legitimate activity has varied "
    "amounts, varied payees, normal spacing, and no threshold-hugging. Judge ONLY from the "
    "evidence; a small count (even 2) can be structuring if the pattern is clear. "
    'Respond with STRICT JSON: {"determination":"CONFIRMED"|"CLEARED","reason":"<one sentence>"}. '
    "CONFIRMED = structuring is occurring (freeze the session); CLEARED = legitimate (release)."
)

_COLLUSION_SYS = (
    "You are an AGENT-COLLUSION / control-evasion detector inside a multi-agent governance "
    "platform. You receive a stream of agent-to-agent messages from ONE session. Collusion is "
    "two or more agents coordinating to push an action past a governance control AFTER it was "
    "flagged/denied — e.g. repeated re-requests, rewording the same intent to dodge a guardrail, "
    "or splitting a blocked action into pieces. Legitimate coordination is one-shot delegation "
    "with no re-attempts after a denial. Judge ONLY from the evidence. "
    'Respond with STRICT JSON: {"determination":"CONFIRMED"|"CLEARED","reason":"<one sentence>"}. '
    "CONFIRMED = collusion/evasion is occurring (freeze the session); CLEARED = normal coordination."
)


def _render_payments(payments) -> str:
    lines = ["RECENT PAYMENTS (this session):"]
    for p in payments:
        try:
            amt = float(p.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amt = 0.0
        lines.append(f"  ${amt:,.0f} -> {p.get('recipient', '') or '<unknown>'}")
    return "\n".join(lines)


_CONFLICT_SYS = (
    "You are an AGENT-CONFLICT / DEADLOCK detector inside a multi-agent governance "
    "platform. You receive a multi-agent deliberation transcript from ONE session. A "
    "deadlock requiring human arbitration is a persistent split with NO convergence "
    "across rounds (e.g. a hung majority that repeats). A healthy deliberation reaches "
    "a clear majority/consensus within the rounds shown. Judge ONLY from the evidence. "
    'Respond with STRICT JSON: {"determination":"CONFIRMED"|"CLEARED","reason":"<one sentence>"}. '
    "CONFIRMED = deadlock needs human review (raise HITL); CLEARED = resolved (no action)."
)


def _render_a2a(messages) -> str:
    lines = ["AGENT-TO-AGENT MESSAGES (this session, in order):"]
    for i, m in enumerate(messages, 1):
        lines.append(f"  {i:>2}. {str(m)[:240]}")
    return "\n".join(lines)


def _render_conflict(messages) -> str:
    lines = ["MULTI-AGENT DELIBERATION TRANSCRIPT (this session, in order):"]
    for i, m in enumerate(messages, 1):
        lines.append(f"  {i:>2}. {str(m)[:240]}")
    return "\n".join(lines)


def _parse_determination(text: str):
    raw = text or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            det = str(d.get("determination", "")).upper()
            if det in ("CONFIRMED", "CLEARED"):
                return det, str(d.get("reason", ""))[:160]
        except Exception:
            pass
    up = raw.upper()
    if "CONFIRMED" in up:
        return "CONFIRMED", ""
    if "CLEARED" in up:
        return "CLEARED", ""
    return "?", ""


async def _detect(llm_call, system, user):
    try:
        raw = await llm_call(system, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[emergent-loop] detector LLM error: %s", exc)
        return "?", "llm_error"
    return _parse_determination(raw)


async def _detect_structuring(llm_call, payments):
    return await _detect(llm_call, _STRUCTURING_SYS, _render_payments(payments))


async def _detect_collusion(llm_call, messages):
    return await _detect(llm_call, _COLLUSION_SYS, _render_a2a(messages))


async def _detect_conflict(llm_call, messages):
    return await _detect(llm_call, _CONFLICT_SYS, _render_conflict(messages))


# Detector registry. Each emergent-risk CLASS maps to: the input stream key in the
# flagged-session record, the detector coroutine, a human label, and a write-back MODE:
#   "freeze" — trip the circuit-breaker on CONFIRMED (runaway containment).
#   "hitl"   — raise human arbitration via the directive queue on CONFIRMED (no freeze;
#              conflict is a deliberation deadlock, resolved by a reviewer, not contained).
_DETECTORS = {
    "structuring": {"stream": "payments",      "fn": _detect_structuring, "label": "structuring", "mode": "freeze", "node": "structuring-detector"},
    "collusion":   {"stream": "a2a",           "fn": _detect_collusion,   "label": "collusion",   "mode": "freeze", "node": "collusion-detector"},
    "conflict":    {"stream": "conflict_a2a",  "fn": _detect_conflict,    "label": "conflict",    "mode": "hitl",   "node": "conflict-detector"},
}


async def _escalate_to_advisory(client, coordinator_url, pe_url, app_id, sid, node, label, reason):
    """Hand a CONFIRMED specialist finding to the ADVISORY agent (the Coordinator):
    POST /coordinate with an explicit ALERT verdict so it synthesises Tier 1-4
    interventions and writes a governance alert to PAP (Alerts / Trust Radar) — without
    needing any pre-existing Sentinel verdict for this session. /coordinate returns 202
    immediately (LLM synthesis runs in its background task), so this never stalls the loop."""
    if coordinator_url:
        try:
            await client.post(
                f"{coordinator_url.rstrip('/')}/coordinate",
                json={
                    "session_id": sid, "app_id": app_id,
                    "sentinel_verdict": "ALERT",
                    "sentinel_checks": [label],
                    "sentinel_reasoning": (reason or f"{label} confirmed by Sentinel specialist")[:400],
                    "pattern_type": label,
                    "confidence": 0.95,
                },
            )
            logger.warning("[emergent-loop] session=%s %s -> Advisory (Tier 1-4 escalation)", sid, label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[emergent-loop] Advisory escalation failed for %s: %s", sid, exc)
    # Platform graph: specialist detector -> Advisory handoff (alert tier).
    await _pa.emit(pe_url, node, "coordinator",
                   f"{label} finding escalated to Advisory", session_id=sid, outcome="alert")


async def _adjudicate_class(client, pe_url, coordinator_url, app_id, sid, risk, risk_class, llm_call):
    """Run one class's agentic detector on its stream and write the verdict back."""
    spec = _DETECTORS[risk_class]
    stream = risk.get(spec["stream"]) or []
    if not stream:
        return None
    det, reason = await spec["fn"](llm_call, stream)
    logger.info("[emergent-loop] session=%s class=%s detector=%s reason=%r",
                sid, risk_class, det, (reason or "")[:80])
    # Platform Agent Activity: Sentinel dispatched this session to the always-on
    # specialist detector node — render the edge + the detector's determination as
    # its Trust-Radar outcome tile (CONFIRMED=alert, CLEARED=clear).
    await _pa.emit(pe_url, _pa.SENTINEL_NODE, spec.get("node", risk_class),
                   f"{risk_class} adjudication: {det} ({(reason or '')[:80]})",
                   session_id=sid, outcome=_pa.outcome_for(det))
    if det == "CONFIRMED" and spec["mode"] == "freeze":
        await client.post(
            f"{pe_url}/internal/session-freeze",
            json={"session_id": sid, "app_id": app_id, "frozen": True,
                  "reason": (f"{spec['label']} confirmed by Sentinel: "
                             f"{reason or risk.get('risk_reason', '')}")[:200]},
        )
        logger.warning("[emergent-loop] session=%s %s CONFIRMED by agent -> FROZEN", sid, risk_class)
    elif det == "CONFIRMED" and spec["mode"] == "hitl":
        # Conflict/deadlock → human arbitration via the directive queue (not a freeze).
        await client.post(
            f"{pe_url}/v1/session/{sid}/directive",
            json={"action": "require_human_review", "category": "agent_arbitration",
                  "source": "sentinel",
                  "reason": (f"Agent deadlock confirmed by Sentinel: "
                             f"{reason or risk.get('risk_reason', '')}")[:240],
                  "metadata": {"emergent_conflict": True, "risk_class": "conflict"}},
        )
        # Mark the class adjudicated so the loop stops re-firing the directive each tick.
        await client.post(
            f"{pe_url}/internal/session-verdict",
            json={"session_id": sid, "app_id": app_id, "verdict": "CLEAR",
                  "risk_class": risk_class, "reason": "conflict arbitration raised (HITL)"},
        )
        logger.warning("[emergent-loop] session=%s conflict CONFIRMED by agent -> HITL directive raised", sid)
    elif det == "CLEARED":
        await client.post(
            f"{pe_url}/internal/session-verdict",
            json={"session_id": sid, "app_id": app_id, "verdict": "CLEAR",
                  "risk_class": risk_class,
                  "reason": f"Sentinel cleared deterministic {spec['label']} flag (false positive)"},
        )
        logger.info("[emergent-loop] session=%s %s CLEARED by agent", sid, risk_class)
    # Every CONFIRMED finding (freeze OR hitl mode) is escalated to the Advisory agent,
    # which synthesises the graded Tier 1-4 response on Trust Radar / Alerts.
    if det == "CONFIRMED":
        await _escalate_to_advisory(client, coordinator_url, pe_url, app_id, sid,
                                    spec.get("node", risk_class), spec["label"], reason)
    return det


async def run_emergent_loop(pe_url: str, llm_call, apps, interval: float, http_timeout: float,
                            detectors=None, coordinator_url: str = ""):
    """Background task: poll the PE risk store, adjudicate flagged sessions, write back.

    Discovery is RISK-STORE-driven (GET /internal/flagged-sessions): the PE returns
    exactly the live sessions its deterministic gate flagged WATCH/CONFIRMED and not
    yet frozen, with each session's per-class flags and the relevant streams (payment
    stream for structuring, A2A stream for collusion) inline. This sees every flagged
    session regardless of evaluation path (instrumented /v1/evaluate OR headless
    /v1/app/evaluate) — the audit firehose did not.

    `detectors` is the set of enabled classes (default {structuring, collusion}); each
    can be toggled independently via env (see main.py).
    """
    if llm_call is None:
        logger.warning("[emergent-loop] no LLM available — emergent detector loop DISABLED.")
        return
    enabled = {c for c in (detectors or {"structuring", "collusion"}) if c in _DETECTORS}
    logger.info("[emergent-loop] started — apps=%s interval=%ss detectors=%s",
                apps, interval, sorted(enabled))
    if _pa.enabled():
        await _pa.register()  # register platform agents under the trust-platform lens
        logger.warning("[emergent-loop] platform Agent Activity ENABLED (app=trust-platform)")
    while True:
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                for app_id in apps:
                    try:
                        r = await client.get(
                            f"{pe_url}/internal/flagged-sessions",
                            params={"app_id": app_id},
                        )
                        r.raise_for_status()
                        flagged = r.json().get("sessions", [])
                    except Exception:  # noqa: BLE001
                        continue
                    if flagged:
                        logger.info("[emergent-loop] app=%s flagged-sessions=%d", app_id, len(flagged))
                        # Platform Agent Activity: the PE gate handed flagged work to Sentinel
                        # (deterministic WATCH tier until a specialist adjudicates).
                        await _pa.emit(pe_url, _pa.GATE_NODE, _pa.SENTINEL_NODE,
                                       f"gate flagged {len(flagged)} session(s) in {app_id}",
                                       outcome="watch")
                    for risk in flagged:
                        sid = risk.get("session_id")
                        if not sid or risk.get("frozen"):
                            continue
                        # Dispatch each flagged class to its detector. Prefer the
                        # per-class risk_flags; fall back to the rollup verdict for
                        # forward/backward compatibility.
                        flags = risk.get("risk_flags") or {}
                        classes = [c for c, v in flags.items()
                                   if str(v).upper() in ("WATCH", "CONFIRMED")]
                        if not classes and str(risk.get("risk_verdict", "")).upper() in ("WATCH", "CONFIRMED"):
                            classes = ["structuring"]  # legacy record without per-class flags
                        for risk_class in classes:
                            if risk_class not in enabled:
                                continue
                            try:
                                det = await _adjudicate_class(client, pe_url, coordinator_url, app_id, sid, risk, risk_class, llm_call)
                                if det == "CONFIRMED":
                                    break  # session now frozen — stop adjudicating it
                            except Exception as wexc:  # noqa: BLE001
                                logger.warning("[emergent-loop] %s write-back failed for %s: %s",
                                               risk_class, sid, wexc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[emergent-loop] tick error: %s", exc)
        await asyncio.sleep(interval)
