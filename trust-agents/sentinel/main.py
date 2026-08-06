"""
Sentinel Agent — FastAPI service, port 3012.

Architecture
────────────
The Sentinel Agent sits entirely outside the SDK and Policy Engine.
It is triggered by the PE via an async webhook (POST /evaluate) and
performs cross-turn blind-spot analysis that the per-turn PE evaluation
cannot see (salami structuring, session escalation patterns, jailbreak
fragmentation).

It is DETECT-ONLY: it never modifies a verdict already returned to the
calling app, never blocks a request, and never talks to the app or SDK.

Two-pass pipeline (in background asyncio task):
  1. Pull last-N events from PE:  GET http://PE:3003/v1/audit/events
  2. Deterministic pre-checks    (policy.yaml thresholds)
  3. LLM judge                   (Claude Haiku / Groq / heuristic fallback)
  4. Persist SentinelVerdict     (in-memory store, keyed by session_id)

Endpoints
─────────
  POST /evaluate                — PE webhook receiver (immediate 200 ACK)
  GET  /verdicts/{session_id}   — latest verdict for a session
  GET  /verdicts                — all stored verdicts (newest first)
  GET  /health                  — liveness check
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Path & env ────────────────────────────────────────────────────────────────
_svc_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_svc_root))          # local imports (schemas, store, evaluator)

load_dotenv(_svc_root / ".env")                    # sentinel/.env  (wins)
load_dotenv(_svc_root.parent / ".env")             # trust-agents/.env  (LLM_BASE_URL, LLM_MODEL)
load_dotenv(_svc_root.parent.parent / ".env")      # repo root fallback

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Required-config validation ──────────────────────────────────────────────
# Peer-service URLs have NO silent fallback — a missing one aborts startup with a
# clear error instead of defaulting to a dead localhost port. (The only legitimate
# fallback in this platform is the LLM provider chain.)
_MISSING_ENV: list = []

def _require_env(name: str, why: str = "") -> str:
    val = os.getenv(name, "").strip()
    if not val:
        _MISSING_ENV.append(f"{name}" + (f"  — {why}" if why else ""))
    return val

def _abort_if_missing_env() -> None:
    if _MISSING_ENV:
        logger.critical(
            "FATAL: required configuration missing — refusing to start.\n"
            "Set these (environment / .env — there is no localhost fallback):\n  - "
            + "\n  - ".join(_MISSING_ENV)
        )
        raise SystemExit(1)

# ── Configuration ──────────────────────────────────────────────────────────────
# REQUIRED — Sentinel pulls audit events from the Policy Engine; useless without it.
PE_URL           = _require_env("SENTINEL_PE_URL", "Sentinel pulls audit events from the Policy Engine")
# Optional feature: empty = Coordinator auto-escalation disabled (explicit off-switch).
COORDINATOR_URL  = os.getenv("SENTINEL_COORDINATOR_URL",  "").strip() or None
HOST             = os.getenv("SENTINEL_HOST",             "0.0.0.0")   # bind default — OK
PORT             = int(os.getenv("SENTINEL_PORT",         "3012"))     # canonical bind port
_abort_if_missing_env()
if COORDINATOR_URL is None:
    logger.warning("[Sentinel] FEATURE DISABLED: SENTINEL_COORDINATOR_URL unset — auto-escalation to Coordinator is OFF.")
POLICY_PATH      = Path(os.getenv("SENTINEL_POLICY_PATH", str(_svc_root / "policy.yaml")))
HTTP_TIMEOUT     = float(os.getenv("SENTINEL_HTTP_TIMEOUT", "5"))

# ── Load policy ───────────────────────────────────────────────────────────────
try:
    with open(POLICY_PATH, "r", encoding="utf-8") as _fh:
        _policy = yaml.safe_load(_fh)
    logger.info(
        f"Policy loaded: {POLICY_PATH}  "
        f"pre_checks={len(_policy.get('pre_checks', []))}  "
        f"pull_limit={_policy.get('pull_limit', 20)}"
    )
except FileNotFoundError:
    logger.critical(f"Policy file not found: {POLICY_PATH}")
    sys.exit(1)
except Exception as e:
    logger.critical(f"Failed to load policy: {e}")
    sys.exit(1)

# ── Core components ───────────────────────────────────────────────────────────
from store     import VerdictStore
from evaluator import SentinelEvaluator
from schemas   import EvaluateAck, SyncEvalRequest, SyncEvalResponse, VerdictResponse, WebhookPayload

_verdict_store = VerdictStore()
_evaluator     = SentinelEvaluator(
    policy          = _policy,
    pe_url          = PE_URL,
    store           = _verdict_store,
    http_timeout    = HTTP_TIMEOUT,
    coordinator_url = COORDINATOR_URL,   # None → auto-escalation disabled
)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "RAI Trust Platform — Sentinel Agent",
    version     = "0.1.0",
    description = (
        "Cross-turn blind-spot detection agent. "
        "Triggered by PE webhook; analyses sessions the per-turn PE evaluation cannot see."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
#  Phase 2 — continuous emergent-risk detector loop (background task)
#  Off the critical path: polls the PE per-session risk records, runs the agentic
#  detector on deterministically-flagged sessions (WATCH/CONFIRMED), and trips the
#  circuit-breaker on CONFIRMED / clears false positives. Reuses the evaluator's
#  LLM provider chain. Disabled cleanly when no LLM is available.
# ─────────────────────────────────────────────────────────────────────────────
# Module-level reference so the background task is NOT garbage-collected
# (asyncio.create_task returns a task that must be kept referenced, else the
#  coroutine can be GC'd and silently never run).
_emergent_task = None
_policy_reviewer_task = None


@app.on_event("startup")
async def _start_emergent_loop():
    global _emergent_task
    try:
        from emergent_loop import run_emergent_loop
        _apps = [a.strip() for a in os.getenv("SENTINEL_WATCH_APPS", "mybank").split(",") if a.strip()]
        _interval = float(os.getenv("SENTINEL_EMERGENT_INTERVAL_S", "5"))
        # Enabled emergent-risk detector classes (toggle each independently).
        # structuring + collusion freeze the breaker; conflict (async) is opt-in and
        # routes via the PE's PE_A2A_SYNC_SENTINEL switch (Phase 2c).
        _detectors = {d.strip() for d in os.getenv(
            "SENTINEL_DETECTORS", "structuring,collusion").split(",") if d.strip()}
        # Advisory (Coordinator) URL — emergent CONFIRMED findings escalate here for
        # Tier 1-4 synthesis onto Trust Radar / Alerts. Empty disables escalation.
        _coord_url = os.getenv("COORDINATOR_URL",
                               os.getenv("SENTINEL_COORDINATOR_URL", "http://127.0.0.1:3013"))
        _emergent_task = asyncio.create_task(
            run_emergent_loop(PE_URL, _evaluator._llm_call, _apps, _interval, HTTP_TIMEOUT,
                              detectors=_detectors, coordinator_url=_coord_url)
        )
        logging.getLogger("sentinel.emergent").warning(
            "[startup] emergent-risk detector loop scheduled (apps=%s, interval=%ss, detectors=%s, llm=%s)",
            _apps, _interval, sorted(_detectors), _evaluator._llm_call is not None,
        )
        # Policy Reviewer — standing specialist that audits the governance config and
        # escalates coverage-gap findings to the Advisory agent (Tier-3 POLICY_CHANGE).
        from policy_reviewer import run_policy_reviewer
        _pap_url = os.getenv("PAP_URL", "http://127.0.0.1:3002")
        _pr_interval = float(os.getenv("POLICY_REVIEWER_INTERVAL_S", "60"))
        global _policy_reviewer_task
        _policy_reviewer_task = asyncio.create_task(
            run_policy_reviewer(PE_URL, _pap_url, _coord_url, _apps, _pr_interval, HTTP_TIMEOUT)
        )
        logging.getLogger("sentinel.policy_reviewer").warning(
            "[startup] policy-reviewer scheduled (interval=%ss, pap=%s)", _pr_interval, _pap_url)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("sentinel.emergent").exception("[startup] failed to schedule emergent loop: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
#  POST /evaluate   — PE webhook receiver
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/evaluate", response_model=EvaluateAck, status_code=202)
async def receive_webhook(payload: WebhookPayload):
    """
    Webhook endpoint called by the Policy Engine after each governance evaluation
    that meets a trigger condition (COERCIVE verdict OR every N turns).

    Returns HTTP 202 immediately — the two-pass analysis runs in a background task.
    """
    logger.info(
        f"[/evaluate] Received webhook — "
        f"interaction={payload.interaction_id[:8]}  "
        f"session={payload.session_id}  "
        f"trigger={payload.trigger_reason}  "
        f"verdict={payload.verdict}"
    )

    # Fire-and-forget: run the two-pass pipeline in the background
    asyncio.create_task(_run_analysis(payload))

    return EvaluateAck(
        status         = "accepted",
        interaction_id = payload.interaction_id,
        session_id     = payload.session_id,
    )


async def _run_analysis(payload: WebhookPayload) -> None:
    """Background task: run the full Sentinel evaluation pipeline."""
    try:
        await _evaluator.evaluate(payload)
    except Exception as exc:
        logger.exception(
            f"[Sentinel] Unhandled error in background analysis "
            f"for interaction={payload.interaction_id}: {exc}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  POST /evaluate-sync   — PE-initiated inline conflict/deadlock classification
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/evaluate-sync", response_model=SyncEvalResponse)
async def evaluate_sync(request: SyncEvalRequest):
    """
    Synchronous conflict/deadlock evaluation called by the Policy Engine
    when accumulated agent_to_agent messages reach the configured cap
    (A2A_HITL_THRESHOLD, default 6 messages = 3 full back-and-forth cycles).

    Returns a verdict synchronously (blocking).  The PE attaches a
    REQUIRE_HUMAN_REVIEW directive to the current response when NORMATIVE.

    Design
    ------
    Completely decoupled from the async observer path (POST /evaluate):
      Sync mode    → PE-initiated, Sentinel acts as pure evaluator.
                     No VerdictStore write.  No Trust Radar post.
                     No background tasks.
      Async mode   → Fire-and-forget webhook, Trust Radar posting as usual.
                     Independent trigger conditions.  No deduplication needed.
    """
    logger.info(
        f"[/evaluate-sync] Received — "
        f"session={request.session_id}  "
        f"messages={len(request.a2a_messages)}  "
        f"options={len(request.proposed_options or [])}"
    )
    result = await _evaluator.evaluate_sync(request)
    # Conflict-node visibility (sync mode): on a confirmed deadlock (NORMATIVE), Sentinel
    # itself lights up the conflict-detector node and escalates to Advisory — so the
    # specialist+Advisory chain shows in the DEFAULT (sync) card-denial demo. Sentinel
    # owns this emission (no PE coupling); best-effort, never affects the returned verdict.
    try:
        if str(getattr(result, "verdict", "")).upper() == "NORMATIVE":
            await _emit_conflict_to_advisory(request.session_id, getattr(result, "reasoning", "") or "")
    except Exception as _cf_exc:  # noqa: BLE001
        logging.getLogger("sentinel.platform_activity").debug("[conflict-emit] skipped: %s", _cf_exc)
    return result


async def _emit_conflict_to_advisory(session_id: str, reasoning: str) -> None:
    """On a confirmed sync deadlock: light up the conflict-detector → Advisory edges on the
    platform graph. The Advisory ALERT itself is raised by Sentinel's existing auto-escalation
    to the Coordinator (verdict==ALERT) — we do NOT POST /coordinate here, to avoid a duplicate
    governance alert. This is visualisation only."""
    import platform_activity as _pa
    sid = session_id or "conflict-sync"
    await _pa.emit(PE_URL, _pa.SENTINEL_NODE, "conflict-detector",
                   f"deadlock confirmed: {reasoning[:80]}", session_id=sid, outcome="alert")
    await _pa.emit(PE_URL, "conflict-detector", "coordinator",
                   "deadlock -> human arbitration", session_id=sid, outcome="alert")


# ─────────────────────────────────────────────────────────────────────────────
#  POST /internal/detect-sync   — time-boxed inline emergent-risk adjudication (Phase 3)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/internal/detect-sync")
async def detect_sync(payload: dict):
    """
    Run ONE Phase-0 specialist detector inline and return its determination fast.

    Called by the PE on a deterministic WATCH when sync fast-track is enabled: the PE
    gives a hard time budget (its own httpx timeout) and applies a tier-strict fallback
    if we do not answer in time. Body: {risk_class, stream}. stream = payment dicts
    (structuring) or A2A message strings (collusion/conflict).

    Returns {determination: CONFIRMED|CLEARED|?, reason, latency_ms, risk_class}.
    """
    import time as _t
    from emergent_loop import _detect_structuring, _detect_collusion, _detect_conflict

    risk_class = str(payload.get("risk_class", "structuring"))
    stream     = payload.get("stream") or []
    t0 = _t.perf_counter()
    if _evaluator._llm_call is None:
        return {"determination": "?", "reason": "no_llm", "latency_ms": 0.0, "risk_class": risk_class}
    _fn = {"structuring": _detect_structuring,
           "collusion":   _detect_collusion,
           "conflict":    _detect_conflict}.get(risk_class)
    if _fn is None:
        return {"determination": "?", "reason": "unknown_class", "latency_ms": 0.0, "risk_class": risk_class}
    det, reason = await _fn(_evaluator._llm_call, stream)
    dt = (_t.perf_counter() - t0) * 1000.0
    logger.info("[/internal/detect-sync] class=%s det=%s latency_ms=%.0f", risk_class, det, dt)
    return {"determination": det, "reason": reason, "latency_ms": round(dt, 1), "risk_class": risk_class}


# ─────────────────────────────────────────────────────────────────────────────
#  GET /verdicts/{session_id}   — latest verdict for one session
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/verdicts/{session_id}", response_model=VerdictResponse)
def get_verdict(session_id: str):
    """
    Return the latest Sentinel verdict for a session.

    Called by the PAP dashboard to surface Sentinel results alongside the
    per-turn PE governance trace.

    Returns 200 with ``found=false`` when the session has not yet been
    analysed — the caller should not treat this as an error.
    """
    verdict = _verdict_store.get(session_id)
    return VerdictResponse(
        session_id = session_id,
        found      = verdict is not None,
        verdict    = verdict,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GET /verdicts   — all stored verdicts
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/verdicts")
def list_verdicts():
    """
    Return all Sentinel verdicts stored in this session, newest first.

    Useful for PAP dashboard overview panels and debugging.
    """
    verdicts = _verdict_store.all()
    return {
        "total":    len(verdicts),
        "verdicts": [v.model_dump() for v in verdicts],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  GET /health
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    """Liveness check — confirms service is running and policy is loaded."""
    return {
        "status":            "healthy",
        "pe_url":            PE_URL,
        "coordinator_url":   COORDINATOR_URL,
        "pre_checks":        len(_policy.get("pre_checks", [])),
        "pull_limit":        _policy.get("pull_limit", 20),
        "verdicts_stored":   len(_verdict_store),
        "llm_active":        _evaluator._llm_call is not None,
        "auto_escalate":     COORDINATOR_URL is not None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  RAI Trust Platform — Sentinel Agent")
    logger.info(f"  Port   : {PORT}")
    logger.info(f"  PE URL : {PE_URL}")
    logger.info(f"  Policy : {POLICY_PATH}")
    logger.info("=" * 60)

    uvicorn.run(
        "main:app",
        host   = HOST,
        port   = PORT,
        reload = False,
        log_level = "info",
        timeout_graceful_shutdown = 0,
    )
