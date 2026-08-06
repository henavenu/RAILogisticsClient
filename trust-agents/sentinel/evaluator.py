"""
SentinelEvaluator — two-pass cross-turn analysis engine.

Two observation planes
──────────────────────
  RAI Pillars / Dimensions  — per-turn signals from the Policy Engine
                              (surfaced as rai_pillar + rai_dimensions)
  Behavioural Dimensions    — cross-turn patterns detected here
                              (surfaced as pattern_type)

Pass 1  Deterministic pre-checks
        Generic metric primitives (count / ratio / stdev /
        dimension_concentration / scope_check) computed over the last N
        events from the PE audit trail.  Thresholds and check definitions
        live in policy.yaml — no code changes needed to tune them.

Pass 2  LLM judge (narrative enrichment only)
        Fires ONLY when at least one pre-check exceeds its threshold.
        Provider priority: Google Vertex AI → Azure OpenAI → Groq → Anthropic →
        Ollama → heuristic. (Ollama is local-dev only.)
        The LLM does NOT re-decide verdict or reclassify pattern — both
        are determined deterministically in Pass 1 and Phase A.
        The LLM adds: rai_pillar, rai_dimensions, agents_implicated,
        evidence_turns, confidence, and a 2-sentence operator narrative.

Two-phase store
───────────────
Phase A — heuristic verdict + deterministic pattern stored immediately
          (<1 s) so the dashboard shows something as soon as the webhook
          arrives.  pattern_type is set here and never overwritten.
Phase B — LLM judge runs in a background asyncio task; on completion the
          stored verdict is patched with pillar / dimensions / narrative.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from schemas import FiredCheck, SentinelVerdict, SyncEvalRequest, SyncEvalResponse, WebhookPayload
from store import VerdictStore

logger = logging.getLogger(__name__)

# ── LLM client factories ──────────────────────────────────────────────────────

def _try_anthropic(model: str, max_tokens: int):
    try:
        import anthropic, os
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return None
        client = anthropic.AsyncAnthropic(api_key=key)
        logger.info(f"[SentinelEvaluator] LLM provider: Anthropic ({model})")

        async def call(system: str, user: str) -> str:
            resp = await client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                temperature=0,   # deterministic verdicts — same input → same classification
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text.strip()
        return call
    except ImportError:
        return None


def _try_azure_openai(max_tokens: int):
    """
    Azure OpenAI — for cloud deployments that only have Azure keys (no Anthropic).

    Requires AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_DEPLOYMENT_NAME
    (OPENAI_API_VERSION optional). The Azure deployment name is passed as the
    ``model`` argument, per the Azure SDK convention. Same env var names MyBank's
    graph.py already uses, so one set of Azure secrets covers app + trust agents.
    """
    try:
        import os
        from openai import AsyncAzureOpenAI
        key        = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        endpoint   = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "").strip()
        if not (key and endpoint and deployment):
            return None
        api_version = os.getenv("OPENAI_API_VERSION", "2024-08-01-preview")
        client = AsyncAzureOpenAI(
            api_key=key, azure_endpoint=endpoint, api_version=api_version,
        )
        logger.info(f"[SentinelEvaluator] LLM provider: Azure OpenAI ({deployment})")

        async def call(system: str, user: str) -> str:
            resp = await client.chat.completions.create(
                model=deployment, max_tokens=max_tokens,
                temperature=0,   # deterministic verdicts (gpt-4o family; drop temp for o-series reasoning models)
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
            )
            return resp.choices[0].message.content.strip()
        return call
    except ImportError:
        return None


def _try_groq(model: str, max_tokens: int):
    try:
        from groq import AsyncGroq
        import os
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            return None
        client = AsyncGroq(api_key=key)
        logger.info(f"[SentinelEvaluator] LLM provider: Groq ({model})")

        async def call(system: str, user: str) -> str:
            resp = await client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                temperature=0,   # deterministic verdicts — same input → same classification
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
            )
            return resp.choices[0].message.content.strip()
        return call
    except ImportError:
        return None


def _try_vertex(model: str, max_tokens: int):
    """Google Gemini on Vertex AI — selected when GOOGLE_CLOUD_PROJECT is set.
    Uses the google-genai SDK (langchain_google_genai); passing `project` auto-selects
    the Vertex backend (no API key). NOT
    langchain_google_vertexai: ChatVertexAI builds 'global-aiplatform.googleapis.com'
    for location=global (a host that 404s); ChatGoogleGenerativeAI addresses the
    global endpoint correctly. Auth is Application Default Credentials (no API key):
    Cloud Run service account (roles/aiplatform.user), or `gcloud auth
    application-default login` in dev. location defaults to 'global'."""
    import os
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        return None
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import SystemMessage, HumanMessage
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
        llm = ChatGoogleGenerativeAI(model=model, project=project,
                                     location=location, max_tokens=max_tokens,
                                     temperature=0)
        logger.info(f"[SentinelEvaluator] LLM provider: Google Gemini ({model}) — Vertex/{location}")

        async def call(system: str, user: str) -> str:
            resp = await llm.ainvoke([SystemMessage(content=system),
                                      HumanMessage(content=user)])
            c = resp.content
            return (c if isinstance(c, str) else str(c)).strip()
        return call
    except ImportError:
        return None
    except Exception as exc:
        logger.warning(f"[SentinelEvaluator] Vertex AI init failed ({exc}) — skipping")
        return None


def _try_ollama(base_url: str, model: str, max_tokens: int):
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return None
    logger.info(f"[SentinelEvaluator] LLM provider: Ollama ({model} @ {base_url})")

    async def call(system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model":       model,
                    "max_tokens":  max_tokens,
                    "temperature": 0,   # deterministic verdicts — same input → same classification
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    return call


# ── Deterministic pattern derivation ─────────────────────────────────────────
#
# Maps pre-check names → (pattern_type, pillar_hint) in priority order.
# When multiple checks fire, the first match wins (most specific first).
# pillar_hint = None means the LLM should infer pillar from dimension content.
#
_CHECK_PATTERN_MAP = [
    # (check_name,              pattern_type,              pillar_hint)
    ("scope_violation",         "scope_violation",         "Security"),
    ("agent_circular_dispute",  "agent_conflict",          "Agent Integrity"),
    # Agents coordinating to commit policy violations (linear collusion chain).
    # Higher priority than rapid_escalation so the collusion label wins when
    # both fire on the same A2A-heavy session.
    ("a2a_coercive",            "collusion",               "Agent Integrity"),
    # Dedicated structuring signal from synthetic domestic-transfer audit events.
    # Takes priority over permissive_tool_flood so it wins when both fire.
    ("domestic_transfer_structuring", "structuring",       "Security"),
    ("permissive_tool_flood",   "structuring",             "Security"),
    ("dimension_concentration", "dimensional_escalation",  None),
    ("user_coercive_density",   "enumeration",             "Safety"),
    ("rapid_escalation",        "persistence",             "Safety"),
    ("coercive_burst",          "persistence",             "Safety"),
    ("high_coercive_ratio",     "persistence",             "Safety"),
    # Advisory: persistent cross-border A2A coordination at NORMATIVE severity.
    # Fires for the preferred-transfer HITL scenario — produces WATCH verdict
    # (non-blocking advisory) alongside the HITL intervention in Human Review.
    ("normative_a2a_pattern",   "cross_border_coordination", "Agent Integrity"),
]


def _derive_pattern(fired_checks: List["FiredCheck"]) -> tuple:
    """
    Return (pattern_type, pillar_hint) from fired pre-checks.

    Priority-ordered: the first matching check in _CHECK_PATTERN_MAP wins.
    Returns (None, None) when no checks fired.
    pillar_hint is None when the pillar depends on which dimension concentrated
    (dimension_concentration case) — the LLM infers it from event content.
    """
    fired_names = {c.name for c in fired_checks}
    for check_name, pattern, pillar_hint in _CHECK_PATTERN_MAP:
        if check_name in fired_names:
            return pattern, pillar_hint
    return None, None


# ── Council deliberation detection ─────────────────────────────────────────────
# The card-authorization council's reviewers talk to each other (agent_to_agent).
# Those internal messages trip GENERIC A2A counters (normative_a2a_pattern,
# dimension_concentration) and would mislabel the session on the Trust Radar as
# 'cross_border_coordination'/'dimensional_escalation'. The council deadlock is
# adjudicated by the LLM in evaluate_sync and surfaced via the Human Review (HITL)
# card — it must NOT also raise a (heuristic, mislabeled) radar finding. Same
# markers evaluate_sync's is_council uses, applied to async audit events.
_COUNCIL_MARKERS = (
    "card authorization review", "decision precedence", "transaction-integrity",
    "uphold the denial", "overturn the denial", "payment surge",
)


def _text_is_council(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _COUNCIL_MARKERS)


def _events_are_council(events: List[dict]) -> bool:
    """True if the audit events are a card-authorization council deliberation."""
    return any(_text_is_council(e.get("content_snippet") or "") for e in (events or []))


# ── Metric primitives ─────────────────────────────────────────────────────────

def _build_filter(filter_spec: dict):
    """Return a predicate function from a YAML filter spec dict."""
    def _match(event: dict) -> bool:
        for key, expected in filter_spec.items():
            if str(event.get(key, "")).upper() != str(expected).upper():
                return False
        return True
    return _match


def _compute_count(events: List[dict], filter_fn) -> int:
    return sum(1 for e in events if filter_fn(e))


def _compute_ratio(events: List[dict], filter_fn) -> float:
    if not events:
        return 0.0
    return _compute_count(events, filter_fn) / len(events)


def _compute_stdev(events: List[dict], field: str) -> float:
    vals = [v for v in (e.get(field) for e in events) if v is not None]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _compute_dimension_concentration(events: List[dict], target_dim: str) -> int:
    """
    Measure how strongly a SINGLE PE dimension is being violated across the window.

    target_dim = 'any'  → return the highest PER-DIMENSION violation count, i.e. the
                          most-frequently-violated dimension. This is true concentration:
                          it fires only when ONE policy flags repeatedly. Counting "any
                          violation" instead would be near-ubiquitous and meaningless —
                          every PE call runs ~the same dimension set, so several DIFFERENT
                          dimensions each tripping once is normal noise, not a targeted
                          attack on one control.
    target_dim = 'sanctions_country_reference' → count only events where that specific
                          dimension fired as a violation.

    Each dimension is counted at most once per event.
    """
    from collections import Counter
    per_dim: "Counter[str]" = Counter()
    for e in events:
        dims = e.get("dimension_results") or []
        seen: set = set()
        for d in dims:
            if not d.get("is_violation"):
                continue
            dim = d.get("dimension")
            if target_dim != "any" and dim != target_dim:
                continue
            if dim in seen:
                continue          # count each dimension at most once per event
            seen.add(dim)
            per_dim[dim] += 1
    if not per_dim:
        return 0
    return max(per_dim.values()) if target_dim == "any" else per_dim.get(target_dim, 0)


def _compute_agent_dispute(events: List[dict]) -> int:
    """
    Detect a circular dispute / coordination deadlock among agent_to_agent events.

    Two signatures are recognised (the stronger one wins):

      1. Bidirectional loop — a pair (A→B) AND (B→A) both present in the window.
         Example: domestic_agent → advisor_agent AND advisor_agent → domestic_agent
         means neither agent can reach resolution and the conversation is looping.
         Requires metadata.source_agent and metadata.target_agent.

      2. Repeated multi-agent exchange — the SAME agents re-sending agent_to_agent
         messages across rounds without converging (e.g. a four-reviewer authority
         that re-votes a hung 2-2 split). This is the genuine, VERDICT-INDEPENDENT
         deadlock signal: it fires in real-LLM mode, where each reviewer's PE
         evaluation comes back PERMISSIVE and the NORMATIVE-pattern prechecks stay
         silent. We count the distinct agents (by metadata.agent_name) that appear
         ≥ 2 times; ≥ 2 such agents re-disputing is a coordination deadlock.
         (A single agent repeating — e.g. an orchestrator issuing routing handoffs —
         does NOT qualify, so clean single-round coordination is not flagged.)

    Returns the larger of the two signals (≥ 1 fires the check).
    """
    from collections import Counter
    pairs: set = set()
    agent_counts: "Counter[str]" = Counter()
    for e in events:
        if e.get("role", "").lower() != "agent_to_agent":
            continue
        meta   = e.get("metadata") or {}
        source = meta.get("source_agent", "")
        target = meta.get("target_agent", "")
        if source and target:
            pairs.add((source, target))
        name = meta.get("agent_name", "")
        if name:
            agent_counts[name] += 1

    # Signature 1: pairs where both (A→B) and (B→A) are present
    circular: set = set()
    for (src, tgt) in pairs:
        if (tgt, src) in pairs:
            circular.add(tuple(sorted([src, tgt])))   # deduplicate by sorted key

    # Signature 2: distinct agents that re-sent (≥2 A2A messages). Requires ≥2 such
    # agents so a lone orchestrator's routing handoffs never trip it on their own.
    repeated_agents = sum(1 for _c in agent_counts.values() if _c >= 2)
    repeated_signal = repeated_agents if repeated_agents >= 2 else 0

    return max(len(circular), repeated_signal)


def _compute_scope_violations(events: List[dict], agent_scopes: dict) -> int:
    """
    Count events where an agent used a tool outside its permitted scope.

    Checks both tool_call and agent_to_agent events that carry
    metadata.agent_name and metadata.tool_name — covering both the synthetic
    boundary-excursion audit event (role=tool_call) and any live A2A events
    that the SDK may annotate with these fields.
    Agents not listed in agent_scopes are silently skipped (no scope defined).
    """
    count = 0
    for e in events:
        if e.get("role", "").lower() not in ("tool_call", "agent_to_agent"):
            continue
        meta       = e.get("metadata") or {}
        agent_name = meta.get("agent_name", "")
        tool_name  = meta.get("tool_name",  "")
        if not agent_name or not tool_name:
            continue
        scope = agent_scopes.get(agent_name)
        if scope is None:
            continue   # unknown agent — no scope defined, skip
        permitted = scope.get("permitted_tools", [])
        if tool_name not in permitted:
            count += 1
    return count


# ── LLM output parsing ────────────────────────────────────────────────────────

_VERDICT_RE    = re.compile(r"^VERDICT\s*:\s*(ALERT|WATCH|CLEAR)", re.IGNORECASE | re.MULTILINE)
_PATTERN_RE    = re.compile(r"^PATTERN\s*:\s*(.+)",                re.IGNORECASE | re.MULTILINE)
_PILLAR_RE     = re.compile(r"^PILLAR\s*:\s*(.+)",                 re.IGNORECASE | re.MULTILINE)
_DIMS_RE       = re.compile(r"^DIMENSIONS\s*:\s*(.+)",             re.IGNORECASE | re.MULTILINE)
_AGENTS_RE     = re.compile(r"^AGENTS\s*:\s*(.+)",                 re.IGNORECASE | re.MULTILINE)
_TURNS_RE      = re.compile(r"^EVIDENCE_TURNS\s*:\s*(.+)",         re.IGNORECASE | re.MULTILINE)
_CONFIDENCE_RE = re.compile(r"^CONFIDENCE\s*:\s*([0-9.]+)",        re.IGNORECASE | re.MULTILINE)
_REASONING_RE  = re.compile(r"^REASONING\s*:\s*(.+)",              re.IGNORECASE | re.MULTILINE | re.DOTALL)

_VALID_PATTERNS = {
    # Constrained ops-recognised vocabulary — add new patterns here to extend.
    "enumeration", "structuring", "dimensional_escalation", "role_escalation",
    "scope_violation", "agent_scope_excursion", "agent_crossover", "collusion",
    "credential_harvesting", "prompt_injection", "persistence", "agent_conflict",
    "cross_border_coordination",   # NORMATIVE multi-step A2A (preferred transfer / HITL advisory)
    "none",
}
_VALID_PILLARS = {
    "Safety", "Privacy", "Security", "Compliance", "Transparency", "Fairness", "Agent Integrity",
}


def _parse_list_field(raw: str) -> List[str]:
    """Parse 'a, b, c' or 'none' → list of strings."""
    raw = raw.strip()
    if not raw or raw.lower() in ("none", "n/a", "-"):
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_int_list(raw: str) -> List[int]:
    """Parse '1, 3, 5' or 'none' → list of ints."""
    items = _parse_list_field(raw)
    result = []
    for item in items:
        try:
            result.append(int(item))
        except ValueError:
            pass
    return result


def _extract_verdict(text: str) -> Optional[str]:
    m = _VERDICT_RE.search(text)
    return m.group(1).upper() if m else None


def _parse_structured_output(raw: str) -> dict:
    """
    Parse the LLM's 6-line narrative output into a dict.

    Expected fields: PILLAR / DIMENSIONS / AGENTS / EVIDENCE_TURNS /
                     CONFIDENCE / REASONING
    VERDICT and PATTERN are intentionally NOT parsed — they are determined
    deterministically and must not be overwritten by LLM output.
    Falls back gracefully when any field is missing.
    """
    result: dict = {}

    m = _PILLAR_RE.search(raw)
    if m:
        pillar  = m.group(1).strip()
        matched = next((p for p in _VALID_PILLARS if p.lower() == pillar.lower()), None)
        result["rai_pillar"] = matched or pillar

    m = _DIMS_RE.search(raw)
    if m:
        result["rai_dimensions"] = _parse_list_field(m.group(1))

    m = _AGENTS_RE.search(raw)
    if m:
        result["agents_implicated"] = _parse_list_field(m.group(1))

    m = _TURNS_RE.search(raw)
    if m:
        result["evidence_turns"] = _parse_int_list(m.group(1))

    m = _CONFIDENCE_RE.search(raw)
    if m:
        try:
            result["confidence"] = max(0.0, min(1.0, float(m.group(1))))
        except ValueError:
            pass

    m = _REASONING_RE.search(raw)
    if m:
        result["reasoning"] = m.group(1).strip()

    return result


# ── Evaluator ─────────────────────────────────────────────────────────────────

class SentinelEvaluator:
    """
    Orchestrates the two-pass Sentinel analysis pipeline.

    Parameters
    ----------
    policy          Parsed policy.yaml dict.
    pe_url          Base URL of the Policy Engine (e.g. http://localhost:3000).
    store           VerdictStore instance to persist results.
    http_timeout    Timeout for PE audit pull requests (seconds).
    coordinator_url Optional base URL of the Coordinator Agent.
    """

    def __init__(
        self,
        policy:          Dict[str, Any],
        pe_url:          str,
        store:           VerdictStore,
        http_timeout:    float = 5.0,
        coordinator_url: Optional[str] = None,
    ) -> None:
        self._policy          = policy
        self._pe_url          = pe_url.rstrip("/")
        self._store           = store
        self._http_timeout    = http_timeout
        self._pull_limit      = int(policy.get("pull_limit", 20))
        self._coordinator_url = coordinator_url.rstrip("/") if coordinator_url else None
        self._agent_scopes    = policy.get("agent_scopes", {})

        import os as _os
        judge_cfg        = policy.get("llm_judge", {})
        _anthropic_model = _os.getenv("ANTHROPIC_MODEL",
                           judge_cfg.get("anthropic_model", "claude-sonnet-4-6"))
        _groq_model      = judge_cfg.get("groq_model",      "llama-3.1-8b-instant")
        _max_tokens      = int(judge_cfg.get("max_tokens",   600))

        # Provider priority: Google Vertex (Gemini) → Anthropic → Azure → Groq → heuristic.
        # CLOUD uses Gemini (GOOGLE_CLOUD_PROJECT) or Anthropic; LOCAL uses Groq or
        # Anthropic. Gemini is first so GCP/Cloud Run selects it; to force Anthropic,
        # set ANTHROPIC_API_KEY and leave GOOGLE_CLOUD_PROJECT unset. Each factory
        # returns None when its config is absent, so the chain falls through cleanly;
        # if none match, the heuristic fallback is used.
        _gemini_model = _os.getenv("GEMINI_MODEL", judge_cfg.get("gemini_model", "gemini-2.5-flash"))
        self._llm_call = (
            _try_vertex(_gemini_model, _max_tokens)
            or _try_anthropic(_anthropic_model, _max_tokens)
            or _try_azure_openai(_max_tokens)
            or _try_groq(_groq_model, _max_tokens)
        )
        if self._llm_call is None:
            logger.warning(
                "[SentinelEvaluator] No LLM available. "
                "Heuristic fallback will be used."
            )

        self._system_prompt = judge_cfg.get("system_prompt", "")
        logger.info(
            f"[SentinelEvaluator] Ready — "
            f"PE: {self._pe_url}  pull_limit: {self._pull_limit}  "
            f"pre_checks: {len(policy.get('pre_checks', []))}  "
            f"agent_scopes: {len(self._agent_scopes)}  "
            f"llm_active: {self._llm_call is not None}  "
            f"coordinator: {self._coordinator_url or 'disabled'}"
        )

    # ── Public ─────────────────────────────────────────────────────────────────

    async def evaluate(self, payload: WebhookPayload) -> SentinelVerdict:
        """
        Run the full two-pass pipeline and persist the verdict.

        Always returns a SentinelVerdict even on error (verdict=CLEAR, pull_failed=True).
        """
        session_id = payload.session_id or payload.interaction_id
        logger.info(
            f"[SentinelEvaluator] Evaluating — "
            f"session={session_id}  trigger={payload.trigger_reason}  "
            f"pe_verdict={payload.verdict}  turn={payload.turn_number}"
        )

        # ── Pass 0: Pull audit events from PE ─────────────────────────────────
        events, pull_failed = await self._pull_events(session_id, payload.app_id)

        # ── Pass 1: Deterministic pre-checks ──────────────────────────────────
        fired_checks = self._run_pre_checks(events)

        # Retry pull if no checks fired — PE webhooks can arrive before the
        # triggering event is fully committed to the audit trail (write-ahead
        # log race).  A short pause + re-poll gives the DB time to settle.
        # This specifically fixes the permissive_tool_flood timing race (S06)
        # where all 6 N_TURN webhooks arrive before enough events are visible.
        _PULL_RETRY_MAX   = 3
        _PULL_RETRY_DELAY = 1.0  # seconds between retries
        if not fired_checks and not pull_failed:
            for _retry in range(1, _PULL_RETRY_MAX + 1):
                await asyncio.sleep(_PULL_RETRY_DELAY)
                events, pull_failed = await self._pull_events(session_id, payload.app_id)
                fired_checks = self._run_pre_checks(events)
                logger.info(
                    f"[SentinelEvaluator] Audit-trail retry {_retry}/{_PULL_RETRY_MAX}: "
                    f"events={len(events)} checks_fired={len(fired_checks)} "
                    f"session={session_id}"
                )
                if fired_checks or pull_failed:
                    break

        logger.info(
            f"[SentinelEvaluator] Pre-checks: "
            f"{len(fired_checks)} fired / {len(self._policy.get('pre_checks', []))} total "
            f"(events={len(events)})"
        )

        # ── Council deliberation → HITL-only, never a radar finding ───────────
        # The card-authorization council's reviewers talk to each other (A2A);
        # that internal chatter trips generic counters (normative_a2a_pattern,
        # dimension_concentration) and would mislabel the session as
        # 'cross_border_coordination'/'dimensional_escalation' on the Trust Radar.
        # The deadlock is genuinely adjudicated by the LLM in evaluate_sync and
        # surfaced via Human Review (HITL) — it must NOT also raise a heuristic
        # radar finding. Suppress the radar write and clear anything a pre-council
        # webhook may already have stored for this session. (HITL path untouched.)
        if _events_are_council(events):
            try:
                self._store.delete(session_id)
            except Exception as _del_exc:
                logger.warning(f"[SentinelEvaluator] council radar-clear failed: {_del_exc}")
            logger.info(
                f"[SentinelEvaluator] Council deliberation — radar finding "
                f"suppressed (HITL-only) for session={session_id}"
            )
            return SentinelVerdict(
                session_id     = session_id,
                app_id         = payload.app_id,
                verdict        = "CLEAR",
                trigger_reason = payload.trigger_reason,
                fired_checks   = [],
                llm_used       = False,
                llm_reasoning  = "Council deliberation — adjudicated via Human Review (HITL); not a radar finding.",
                interaction_id = payload.interaction_id,
                event_count    = len(events),
                pull_failed    = pull_failed,
            )

        # ── Pass 2: Two-phase store ────────────────────────────────────────────
        #
        # Phase A — store heuristic verdict immediately so the dashboard always
        #           shows something within <1 s of the webhook arriving.
        # Phase B — LLM judge runs in a background task; enriches the verdict
        #           with narrative reasoning, pillar, dimensions, and evidence.
        #           It does NOT re-decide the verdict or pattern — those are
        #           already determined deterministically here in Phase A.
        #
        hv                     = self._heuristic_verdict(fired_checks) if fired_checks else "CLEAR"
        derived_pattern, \
        pillar_hint            = _derive_pattern(fired_checks) if fired_checks else (None, None)
        heuristic_rsn          = self._build_heuristic_reasoning(
                                     fired_checks, hv, derived_pattern
                                 ) if fired_checks else ""

        sv = SentinelVerdict(
            session_id     = session_id,
            app_id         = payload.app_id,
            verdict        = hv,
            trigger_reason = payload.trigger_reason,
            fired_checks   = fired_checks,
            llm_used       = False,
            llm_reasoning  = heuristic_rsn,
            interaction_id = payload.interaction_id,
            event_count    = len(events),
            pull_failed    = pull_failed,
            pattern_type   = derived_pattern,       # set at Phase A — LLM cannot change
            rai_pillar     = pillar_hint,            # may be None; LLM fills if needed
        )

        # Phase A: persist heuristic verdict immediately.
        # authoritative=False → never downgrade an existing verdict (race guard).
        self._store.put(sv, authoritative=False)
        logger.info(
            f"[SentinelEvaluator] Heuristic verdict stored — "
            f"verdict={hv}  pattern={derived_pattern or '-'}  "
            f"session={session_id}  checks={[c.name for c in fired_checks]}"
        )

        # Phase B: background LLM narrative enrichment + coordinator notification.
        if fired_checks:
            if self._llm_call is not None:
                asyncio.create_task(
                    self._llm_judge_and_patch(
                        sv, events, fired_checks, payload,
                        derived_pattern=derived_pattern,
                        pillar_hint=pillar_hint,
                    )
                )
            elif hv == "ALERT" and self._coordinator_url:
                asyncio.create_task(self._notify_coordinator(sv))

        # Phase C: agent/tool block — fires immediately on ALERT, no LLM needed.
        # Maps the fired pattern to the agent+tool that should be blocked so the
        # PE can enforce on every subsequent evaluate() call within this session.
        if hv == "ALERT":
            asyncio.create_task(self._block_agent_at_pe(sv))

        return sv

    # ── Sync evaluation (PE-initiated inline path) ────────────────────────────
    #
    # Completely decoupled from the async observer path above:
    #   Sync mode    → PE-initiated, pure evaluator response, no VerdictStore
    #                  write, no Trust Radar post.
    #   Async mode   → Fire-and-forget webhook, Trust Radar posting as usual.

    async def evaluate_sync(self, request: SyncEvalRequest) -> SyncEvalResponse:
        """
        Synchronous conflict/deadlock evaluation.

        Called by the Policy Engine (via POST /evaluate-sync) when accumulated
        agent_to_agent messages reach the configured cap.  Sentinel's LLM
        classifies the exchange as PERMISSIVE (normal coordination) or NORMATIVE
        (genuine conflict/deadlock requiring human arbitration).

        Design constraints
        ------------------
        • No VerdictStore write — result is returned directly to the PE caller.
        • No Trust Radar post   — the async observer path fires on its own trigger.
        • No background tasks   — this must complete synchronously before the PE
          can attach the directive to the current evaluate() response.
        • Fail-open             — if the LLM is unavailable, returns PERMISSIVE
          with a warning log so no false-positive HITL interrupt fires.

        Option evaluation (POST-MVP)
        ----------------------------
        When proposed_options are supplied AND the verdict is NORMATIVE, Sentinel's
        LLM picks the best option from the list and returns it as recommended_option.

        # POST-MVP: review whether option evaluation belongs in Sentinel or a
        # dedicated evaluator service.  Built here to unblock the myBank demo.
        """
        session_id = request.session_id
        messages   = request.a2a_messages

        if not messages:
            logger.info(
                f"[SentinelEvaluator.evaluate_sync] No messages supplied — "
                f"returning PERMISSIVE for session={session_id}"
            )
            return SyncEvalResponse(verdict="PERMISSIVE", reasoning="No A2A messages supplied.")

        if self._llm_call is None:
            logger.warning(
                f"[SentinelEvaluator.evaluate_sync] No LLM available — "
                f"returning PERMISSIVE (fail-open) for session={session_id}"
            )
            return SyncEvalResponse(
                verdict   = "PERMISSIVE",
                reasoning = "LLM unavailable — defaulting to PERMISSIVE (fail-open).",
            )

        # ── Build LLM prompt ──────────────────────────────────────────────────
        numbered = "\n".join(
            f"[{i + 1}] {msg}" for i, msg in enumerate(messages)
        )

        system = (
            "You are a Responsible AI governance auditor specialising in multi-agent "
            "system behaviour. You are given the COMPLETE sequence of agent-to-agent "
            "messages from a single negotiation between two agents, and you must judge "
            "the trajectory of the whole exchange — not any message in isolation.\n\n"
            "Classify the exchange as one of:\n"
            "  A) PRODUCTIVE COORDINATION (PERMISSIVE) — each round introduces new "
            "information, narrows the option set, or moves measurably closer to a "
            "resolution the agents can both accept.\n"
            "  B) CONFLICT / DEADLOCK (NORMATIVE) — the agents cannot converge. Treat ANY "
            "one of the following as sufficient evidence of deadlock:\n"
            "     • a candidate, option, or result set that was already evaluated and "
            "rejected reappears in a later round (circular / repeated results);\n"
            "     • three or more rounds of rejection with no new viable candidate "
            "introduced;\n"
            "     • an explicit statement that the search space / portfolio is exhausted, "
            "that no new options remain, or that no mutually acceptable resolution can be "
            "reached through further automated effort;\n"
            "     • the agents restate prior positions without progress.\n\n"
            "Individual messages in a deadlock often look reasonable on their own — judge "
            "the PATTERN across all rounds. When any deadlock signal above is present, you "
            "MUST return NORMATIVE. Reserve PERMISSIVE for exchanges showing genuine "
            "forward progress toward resolution.\n\n"
            "Respond with EXACTLY these lines (no extra text):\n"
            "VERDICT: PERMISSIVE or NORMATIVE\n"
            "PATTERN: one of: agent_conflict | cross_border_coordination | collusion | none\n"
            "REASONING: one or two sentences citing the specific evidence (e.g. which "
            "rounds repeated, or the exhaustion statement)."
        )

        user = (
            f"SESSION: {session_id}\n\n"
            f"AGENT-TO-AGENT MESSAGES ({len(messages)} total):\n{numbered}\n\n"
            "Analyse the above exchange and respond with VERDICT, PATTERN, and REASONING."
        )

        # ── Card-Authorization-Review addendum ────────────────────────────────
        # Detect the four-agent card-denial dispute. This only IDENTIFIES the
        # exchange; it does NOT decide the verdict.
        is_council = any(
            ("card authorization review" in m.lower())
            or ("decision precedence" in m.lower())
            or ("transaction-integrity" in m.lower())
            or ("uphold the denial" in m.lower())
            or ("overturn the denial" in m.lower())
            or ("payment surge" in m.lower())
            for m in messages
        )
        # NON-NEGOTIABLE (real feature): the verdict is the Sentinel's GENUINE
        # judgement from inspecting ALL messages against the deadlock criteria in
        # the base prompt above — it CAN be PERMISSIVE. The A2A cap is only an
        # early trigger to invoke this evaluation; it does not decide the outcome.
        # We no longer force NORMATIVE (the prior `MUST return NORMATIVE` + the
        # post-parse override below have both been removed). The crafted council
        # messages contain genuine deadlock evidence — a fixed split that persists
        # across rounds plus the coordinator's explicit "no mutually acceptable
        # resolution remains" statement — which the base criteria classify as a
        # deadlock on their own, so no hint is needed.
        #
        # DEMO-ONLY / TEMPORARY — POST-MVP TODO: the precedence-ranked RESOLUTION
        # synthesis appended below is an EXTRA responsibility added for the demo.
        # Sentinel should not own resolution arbitration; move it to a dedicated
        # arbitration service post-MVP (cf. _random_pick_card). Remove this addendum
        # and the RESOLUTION handling in the parse block once that service exists.
        if is_council:
            system += (
                "\n\nADDITIONAL CONTEXT — CARD AUTHORIZATION REVIEW: the messages are a "
                "four-agent dispute over a DECLINED card charge (a transaction-integrity/fraud "
                "agent and a risk-policy agent tend to UPHOLD; a revenue agent and a "
                "customer-experience agent tend to OVERTURN). Judge deadlock GENUINELY by the "
                "criteria above — a split that persists across rounds together with an explicit "
                "statement that no automated resolution remains IS a deadlock (NORMATIVE); real "
                "convergence is PERMISSIVE.\n"
                "DEMO-ONLY AID — IF AND ONLY IF your verdict is NORMATIVE, also append a RESOLUTION "
                "line applying the bank's decision precedence in order: (1) compliance & loss "
                "prevention, (2) customer experience, (3) revenue. REVISE ON NEW EVIDENCE: if a "
                "verification update or new evidence (confirmed travel, device/biometric match) "
                "appears that satisfies the compliance concern, OVERTURN with a one-time step-up "
                "rather than uphold. Use ONLY facts present in the messages.\n"
                "Append this line only when NORMATIVE:\n"
                "RESOLUTION: one line — the final decision grounded in the precedence and any new evidence."
            )

        # This verdict gates a HITL interrupt, so a TRANSIENT provider rate-limit
        # (HTTP 429) or overload (503) must NOT silently fail-open to PERMISSIVE —
        # that's exactly how a real deadlock slips through. Retry a few times with
        # backoff on retryable errors; only fail-open for non-retryable errors or
        # once retries are exhausted. (evaluate_sync is async — await directly;
        # run_in_executor would return the unawaited coroutine.)
        raw = None
        _last_exc = None
        for _attempt in range(3):
            try:
                raw = await self._llm_call(system, user)
                break
            except Exception as exc:  # noqa: BLE001
                _last_exc = exc
                _m = str(exc).lower()
                _retryable = ("429" in _m or "rate limit" in _m
                              or "overloaded" in _m or "503" in _m)
                if _retryable and _attempt < 2:
                    _delay = 2.0 * (_attempt + 1)   # 2s, then 4s
                    logger.warning(
                        "[SentinelEvaluator.evaluate_sync] LLM rate-limited/overloaded "
                        "(attempt %d/3) — retrying in %.0fs: %s", _attempt + 1, _delay, exc)
                    await asyncio.sleep(_delay)
                    continue
                break
        if raw is None:
            logger.warning(
                f"[SentinelEvaluator.evaluate_sync] LLM call failed after retries: "
                f"{_last_exc} — returning PERMISSIVE for session={session_id}"
            )
            return SyncEvalResponse(
                verdict   = "PERMISSIVE",
                reasoning = f"LLM error — defaulting to PERMISSIVE (fail-open): {_last_exc}",
            )

        # ── Parse response ────────────────────────────────────────────────────
        verdict_match  = re.search(r"VERDICT\s*:\s*(PERMISSIVE|NORMATIVE)", raw, re.IGNORECASE)
        pattern_match  = re.search(r"PATTERN\s*:\s*(.+)",                   raw, re.IGNORECASE)
        reasoning_match= re.search(r"REASONING\s*:\s*(.+)",                 raw, re.IGNORECASE | re.DOTALL)

        verdict      = verdict_match.group(1).upper()  if verdict_match  else "PERMISSIVE"
        pattern_raw  = pattern_match.group(1).strip()  if pattern_match  else ""
        pattern_type = pattern_raw if pattern_raw.lower() not in ("none", "") else None
        reasoning    = reasoning_match.group(1).strip() if reasoning_match else raw.strip()[:300]

        # ── Card review: fold the (demo-only) RESOLUTION into reasoning ───────────
        # The verdict above is the LLM's GENUINE deadlock judgement — NOT forced.
        # When it genuinely concludes NORMATIVE, the demo-only precedence resolution
        # rides the `reasoning` field, which the PE forwards into the directive
        # `reason` → HITLInterrupt payload → MyBank Human Review card. If the genuine
        # verdict is PERMISSIVE, no resolution is appended and no HITL is raised.
        if is_council and verdict == "NORMATIVE":
            pattern_type = pattern_type or "agent_conflict"
            # The REASONING capture (DOTALL) may have swallowed the RESOLUTION line —
            # strip it so we don't duplicate it when we re-append.
            reasoning    = re.split(r"RESOLUTION\s*:", reasoning, flags=re.IGNORECASE)[0].strip()
            res_match    = re.search(r"RESOLUTION\s*:\s*(.+)", raw, re.IGNORECASE)
            if res_match:
                resolution = res_match.group(1).strip().splitlines()[0].strip()
                reasoning  = f"{reasoning}  |  RECOMMENDED RESOLUTION: {resolution}"

        logger.info(
            f"[SentinelEvaluator.evaluate_sync] "
            f"session={session_id}  verdict={verdict}  "
            f"pattern={pattern_type or 'none'}  "
            f"reasoning={reasoning[:100]}"
        )

        # ── Option selection (MVP TEMPORARY — random pick) ────────────────────
        # MVP TEMPORARY: when a deadlock is detected (NORMATIVE) and proposed_options
        # are supplied, randomly pick one card as the governance recommendation.
        # POST-MVP TODO: replace _random_pick_card with a call to a dedicated
        # card recommendation arbitration service.  Remove this block and
        # _random_pick_card once that service is available.
        recommended_option: Optional[dict] = None
        if verdict == "NORMATIVE" and request.proposed_options:
            recommended_option = self._random_pick_card(
                session_id       = session_id,
                proposed_options = request.proposed_options,
            )

        # NOTE: the council deadlock is surfaced to the operator via the HITL /
        # Human Review card (driven by the SyncEvalResponse the PE attaches to its
        # directive). It is intentionally NOT written to the Trust Radar verdict
        # store here: a session can carry several findings, and the radar's
        # one-verdict-per-session model would flatten/mislabel them (and the label
        # was LLM-provider-dependent). Surfacing per-finding on the radar is a
        # post-MVP redesign (finding-as-first-class record). Card-denial = HITL only.

        return SyncEvalResponse(
            verdict            = verdict,
            pattern_type       = pattern_type,
            reasoning          = reasoning,
            recommended_option = recommended_option,
        )

    def _random_pick_card(
        self,
        session_id:       str,
        proposed_options: List[dict],
    ) -> Optional[dict]:
        """
        Randomly select one card from proposed_options when a deadlock is detected.

        Called synchronously from evaluate_sync when verdict == NORMATIVE and
        proposed_options are present.  Returns a copy of the selected option dict
        with an added _sentinel_rationale key explaining the selection basis.

        # MVP TEMPORARY FEATURE — POST-MVP TODO:
        # This card selection logic does not belong in Sentinel.
        # Move to a dedicated card recommendation arbitration service after MVP.
        # Remove this method and the call site in evaluate_sync once that
        # arbitration service is available.
        """
        if not proposed_options:
            return None
        import random
        idx    = random.randint(0, len(proposed_options) - 1)
        picked = dict(proposed_options[idx])
        picked["_sentinel_rationale"] = (
            "Selected by Sentinel governance arbitrator (random pick — "
            "post-MVP: replace with intelligent selection in arbitration service)"
        )
        logger.info(
            "[SentinelEvaluator._random_pick_card] Randomly selected option %d of %d "
            "for session=%s: card_id=%s",
            idx + 1, len(proposed_options), session_id,
            picked.get("card_id", "unknown"),
        )
        return picked

    async def _pick_option(
        self,
        session_id:       str,
        a2a_messages:     List[str],
        proposed_options: List[dict],
    ) -> Optional[dict]:
        """
        Ask the LLM to pick the most appropriate option from proposed_options.

        Called only when evaluate_sync returns NORMATIVE and options are present.
        Fail-open: returns None on any LLM error so a missing pick never blocks HITL.

        # POST-MVP: review whether option evaluation belongs in Sentinel or a
        # dedicated evaluator service.
        """
        if self._llm_call is None:
            return None

        numbered_msgs = "\n".join(
            f"[{i + 1}] {m}" for i, m in enumerate(a2a_messages)
        )
        options_text = "\n".join(
            f"[OPTION {i + 1}] {opt}" for i, opt in enumerate(proposed_options)
        )

        system = (
            "You are a Responsible AI governance arbitrator.\n\n"
            "You have identified a conflict or deadlock in a multi-agent exchange. "
            "You are now presented with a set of proposed resolution options generated "
            "by the application agents.\n\n"
            "Select the SINGLE best option that resolves the deadlock while best "
            "aligning with the user's original intent.  Respond with EXACTLY:\n"
            "SELECTED_OPTION: <number>\n"
            "RATIONALE: one sentence explaining why.\n\n"
            "Do not select an option that introduces new risks or violates any policies "
            "implied by the agent messages."
        )

        user = (
            f"SESSION: {session_id}\n\n"
            f"AGENT EXCHANGE:\n{numbered_msgs}\n\n"
            f"PROPOSED OPTIONS:\n{options_text}\n\n"
            "Which option best resolves the deadlock?"
        )

        try:
            # Same fix as evaluate_sync: await the async LLM callable directly.
            raw = await self._llm_call(system, user)
        except Exception as exc:
            logger.warning(
                f"[SentinelEvaluator._pick_option] LLM call failed: {exc} — "
                f"returning None for session={session_id}"
            )
            return None

        sel_match = re.search(r"SELECTED_OPTION\s*:\s*(\d+)", raw, re.IGNORECASE)
        if not sel_match:
            logger.warning(
                f"[SentinelEvaluator._pick_option] Could not parse SELECTED_OPTION "
                f"from LLM response for session={session_id}"
            )
            return None

        idx = int(sel_match.group(1)) - 1
        if 0 <= idx < len(proposed_options):
            rationale_match = re.search(r"RATIONALE\s*:\s*(.+)", raw, re.IGNORECASE | re.DOTALL)
            picked = dict(proposed_options[idx])
            picked["_sentinel_rationale"] = (
                rationale_match.group(1).strip() if rationale_match else ""
            )
            logger.info(
                f"[SentinelEvaluator._pick_option] Selected option {idx + 1} "
                f"for session={session_id}: {picked}"
            )
            return picked

        logger.warning(
            f"[SentinelEvaluator._pick_option] SELECTED_OPTION index {idx + 1} "
            f"out of range ({len(proposed_options)} options) — returning None"
        )
        return None

    # ── Background LLM patch ──────────────────────────────────────────────────

    async def _llm_judge_and_patch(
        self,
        sv:              SentinelVerdict,
        events:          List[dict],
        fired_checks:    List[FiredCheck],
        payload:         WebhookPayload,
        derived_pattern: Optional[str] = None,
        pillar_hint:     Optional[str] = None,
    ) -> None:
        """
        Run the LLM narrative enrichment asynchronously and patch the verdict.

        The LLM's job is ONLY to produce:
          - rai_pillar    (which governance pillar is most affected)
          - rai_dimensions (which PE dimensions fired)
          - agents_implicated
          - evidence_turns
          - confidence
          - reasoning     (human-readable narrative for the operator)

        The LLM does NOT re-decide the verdict (heuristic floor stands) and
        does NOT reclassify the pattern (derived deterministically in Phase A).

        On LLM failure the heuristic reasoning from Phase A remains visible.
        """
        llm_reasoning, llm_used, parsed = await self._llm_judge(
            events, fired_checks, payload,
            derived_pattern=derived_pattern,
            pillar_hint=pillar_hint,
        )

        if not llm_used:
            if sv.verdict == "ALERT" and self._coordinator_url:
                asyncio.create_task(self._notify_coordinator(sv))
            return

        # Patch ONLY the narrative / metadata fields.
        # verdict and pattern_type are intentionally NOT overwritten —
        # they were set deterministically in Phase A and are the floor.
        sv.llm_used          = True
        sv.llm_reasoning     = llm_reasoning
        sv.rai_pillar        = parsed.get("rai_pillar") or pillar_hint
        sv.rai_dimensions    = parsed.get("rai_dimensions",    [])
        sv.agents_implicated = parsed.get("agents_implicated", [])
        sv.confidence        = parsed.get("confidence")
        sv.evidence_turns    = parsed.get("evidence_turns",    [])
        # pattern_type stays as derived_pattern — never overwritten by LLM

        self._store.put(sv, authoritative=True)
        logger.info(
            f"[SentinelEvaluator] LLM narrative patched — "
            f"verdict={sv.verdict}  pattern={sv.pattern_type}  "
            f"pillar={sv.rai_pillar}  session={sv.session_id}"
        )

        if sv.verdict == "ALERT" and self._coordinator_url:
            asyncio.create_task(self._notify_coordinator(sv))

    # ── Pattern → block target mapping ───────────────────────────────────────────
    #
    # Defines which agent and tool are blocked when each ALERT pattern fires.
    # Keyed by pattern_type (same values as _CHECK_PATTERN_MAP).
    # Only patterns with a clear, single responsible agent need an entry.
    # Patterns like "persistence" or "enumeration" originate from user behaviour
    # (not a specific agent) and are intentionally omitted — those are handled
    # by session-level escalation in the PE, not agent blocking.
    #
    _PATTERN_BLOCK_MAP: Dict[str, Dict[str, str]] = {
        "structuring": {
            "agent_name": "domestic_agent",
            "tool_name":  "domestic_transfer",
        },
        "scope_violation": {
            # The offending agent is detected dynamically from fired events,
            # but we can't easily extract it here without re-reading events.
            # Default: block nothing — scope violations are already COERCIVE
            # in the PE per-turn verdict; no additional block needed.
        },
        "collusion": {},
        "agent_conflict": {},
    }

    async def _block_agent_at_pe(self, sv: "SentinelVerdict") -> None:
        """
        POST to the PE's /v1/sessions/agent-block endpoint to block the agent
        and/or tool responsible for the ALERT pattern in this session.

        Non-blocking: runs as a background task.  A failure here is logged
        but never degrades the Sentinel verdict or the Trust Radar display.
        """
        pattern    = sv.pattern_type or ""
        block_spec = self._PATTERN_BLOCK_MAP.get(pattern, {})
        agent_name = block_spec.get("agent_name", "")
        tool_name  = block_spec.get("tool_name",  "")

        if not agent_name and not tool_name:
            logger.debug(
                f"[SentinelEvaluator] No block target for pattern={pattern!r} "
                f"— skipping agent-block POST"
            )
            return

        payload = {
            "session_id": sv.session_id,
            "agent_name": agent_name,
            "tool_name":  tool_name,
            "reason":     f"sentinel_alert:{pattern}",
            "pattern":    pattern,
            "blocked_by": "sentinel",
        }
        url = f"{self._pe_url}/v1/sessions/agent-block"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.post(url, json=payload)
            if resp.is_success:
                logger.info(
                    f"[SentinelEvaluator] Agent block posted — "
                    f"session={sv.session_id} agent={agent_name!r} tool={tool_name!r} "
                    f"pattern={pattern!r} status={resp.status_code}"
                )
            else:
                logger.warning(
                    f"[SentinelEvaluator] Agent-block endpoint returned {resp.status_code} "
                    f"for session={sv.session_id}: {resp.text[:200]!r}"
                )
        except Exception as exc:
            logger.warning(
                f"[SentinelEvaluator] Failed to POST agent block to PE: {exc}"
            )

    # ── Coordinator notification ───────────────────────────────────────────────

    async def _notify_coordinator(self, sv: SentinelVerdict) -> None:
        """
        POST to the Coordinator's /coordinate endpoint when verdict == ALERT.
        Passes the full enriched Sentinel verdict so the Coordinator can produce
        pattern-aware, tiered interventions without needing to re-fetch.
        Never raises — a failed notification is logged and dropped.
        """
        payload = {
            "session_id":          sv.session_id,
            "app_id":              sv.app_id,
            "sentinel_verdict":    sv.verdict,
            "sentinel_checks":     [c.name for c in sv.fired_checks],
            "sentinel_reasoning":  sv.llm_reasoning,
            # Behavioural classification fields
            "pattern_type":        sv.pattern_type,
            "rai_pillar":          sv.rai_pillar,
            "rai_dimensions":      sv.rai_dimensions,
            "agents_implicated":   sv.agents_implicated,
            "confidence":          sv.confidence,
            "evidence_turns":      sv.evidence_turns,
        }
        # Coordinator LLM processing takes 20-90s — use a generous timeout so we
        # don't fire-and-forget a genuine connection error vs. normal processing delay.
        _coord_timeout = max(self._http_timeout, 120.0)
        url = f"{self._coordinator_url}/coordinate"
        try:
            async with httpx.AsyncClient(timeout=_coord_timeout) as client:
                resp = await client.post(url, json=payload)
            if resp.is_success:
                logger.info(
                    f"[SentinelEvaluator] Coordinator notified — "
                    f"session={sv.session_id}  status={resp.status_code}"
                )
            else:
                logger.warning(
                    f"[SentinelEvaluator] Coordinator returned {resp.status_code} "
                    f"for session={sv.session_id}"
                )
        except Exception as exc:
            logger.warning(
                f"[SentinelEvaluator] Failed to notify Coordinator: {exc}"
            )

    # ── Pass 0: Pull ──────────────────────────────────────────────────────────

    async def _pull_events(
        self, session_id: str, app_id: Optional[str]
    ) -> Tuple[List[dict], bool]:
        """Pull the last N events from the PE audit trail. Returns (events, pull_failed)."""
        params: Dict[str, Any] = {
            "limit":        self._pull_limit,
            "include_dims": "true",
        }
        if session_id:
            params["session_id"] = session_id
        if app_id:
            params["app_id"] = app_id

        url = f"{self._pe_url}/v1/audit/events"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url, params=params)
            if resp.is_success:
                events = resp.json().get("events", [])
                logger.debug(
                    f"[SentinelEvaluator] Pulled {len(events)} events for session={session_id}"
                )
                return events, False
            else:
                logger.warning(
                    f"[SentinelEvaluator] PE returned {resp.status_code} "
                    f"for session={session_id}: {resp.text[:200]!r}"
                )
                return [], True
        except Exception as exc:
            logger.warning(f"[SentinelEvaluator] Failed to pull events from PE: {exc}")
            return [], True

    # ── Pass 1: Pre-checks ────────────────────────────────────────────────────

    def _run_pre_checks(self, events: List[dict]) -> List[FiredCheck]:
        """Evaluate all pre-checks and return those whose metric >= threshold."""
        fired: List[FiredCheck] = []

        for check in self._policy.get("pre_checks", []):
            name         = check.get("name", "unnamed")
            compute_type = check.get("compute_type", "count")
            threshold    = float(check.get("threshold", 1))
            window       = int(check.get("window", len(events) or 1))
            description  = check.get("description", "")
            filter_spec  = check.get("filter", {})

            window_events = events[:window]   # events are newest-first from PE
            filter_fn     = _build_filter(filter_spec)

            try:
                if compute_type == "count":
                    value = float(_compute_count(window_events, filter_fn))

                elif compute_type == "ratio":
                    value = _compute_ratio(window_events, filter_fn)

                elif compute_type == "stdev":
                    field = check.get("field", "latency_ms")
                    value = _compute_stdev(window_events, field)

                elif compute_type == "dimension_concentration":
                    target_dim = check.get("dimension", "any")
                    value = float(_compute_dimension_concentration(window_events, target_dim))

                elif compute_type == "scope_check":
                    value = float(_compute_scope_violations(window_events, self._agent_scopes))

                elif compute_type == "agent_dispute":
                    value = float(_compute_agent_dispute(window_events))

                elif compute_type == "metadata_count":
                    meta_field = check.get("metadata_field", "")
                    meta_value = check.get("metadata_value", "")
                    value = float(sum(
                        1 for e in window_events
                        if str((e.get("metadata") or {}).get(meta_field, "")).upper()
                        == meta_value.upper()
                    ))

                elif compute_type == "metadata_sum":
                    # Sum a numeric metadata field across events that match an
                    # optional filter (metadata_filter_field == metadata_filter_value).
                    # Correct for BSA §5324 structuring: fires when the running
                    # dollar total of sub-threshold transfers crosses the limit,
                    # regardless of how many individual transactions were made.
                    filter_field = check.get("metadata_filter_field", "")
                    filter_value = check.get("metadata_filter_value", "")
                    sum_field    = check.get("metadata_sum_field", "amount")
                    total = 0.0
                    for _e in window_events:
                        _meta = _e.get("metadata") or {}
                        if filter_field and filter_value:
                            if str(_meta.get(filter_field, "")).upper() != filter_value.upper():
                                continue
                        try:
                            total += float(_meta.get(sum_field, 0) or 0)
                        except (TypeError, ValueError):
                            pass
                    value = total

                else:
                    logger.warning(
                        f"[SentinelEvaluator] Unknown compute_type '{compute_type}' "
                        f"in check '{name}'"
                    )
                    continue

                logger.debug(
                    f"[SentinelEvaluator] check={name} "
                    f"type={compute_type} value={value:.3f} threshold={threshold}"
                )

                if value >= threshold:
                    fired.append(FiredCheck(
                        name=name,
                        value=round(value, 4),
                        threshold=threshold,
                        description=description,
                    ))

            except Exception as exc:
                logger.warning(
                    f"[SentinelEvaluator] Pre-check '{name}' failed (skipped): {exc}"
                )

        return fired

    # ── Pass 2: LLM judge ─────────────────────────────────────────────────────

    async def _llm_judge(
        self,
        events:          List[dict],
        fired_checks:    List[FiredCheck],
        payload:         WebhookPayload,
        derived_pattern: Optional[str] = None,
        pillar_hint:     Optional[str] = None,
    ) -> Tuple[str, bool, dict]:
        """
        Call the LLM for narrative enrichment only.

        Returns (reasoning, llm_used, parsed_fields).

        The LLM receives the already-determined verdict and pattern as context
        so it does not need to re-decide either. It returns 6 fields:
          PILLAR / DIMENSIONS / AGENTS / EVIDENCE_TURNS / CONFIDENCE / REASONING
        """
        if self._llm_call is None:
            return "", False, {}

        checks_lines = "\n".join(
            f"  • {c.name}: {c.value:.3f} ≥ {c.threshold}  ({c.description})"
            for c in fired_checks
        )
        events_lines = self._format_events(events[:15])
        hv           = self._heuristic_verdict(fired_checks)

        user_msg = (
            f"Session: {payload.session_id or 'n/a'}\n"
            f"App: {payload.app_id or 'n/a'}\n"
            f"\n"
            f"Already determined by pre-checks (do not change these):\n"
            f"  Verdict:  {hv}\n"
            f"  Pattern:  {derived_pattern or 'unknown'}\n"
            f"  Pillar hint: {pillar_hint or 'determine from events'}\n"
            f"\n"
            f"Pre-checks that fired:\n{checks_lines}\n"
            f"\nLast {len(events[:15])} audit events (newest first):\n{events_lines}\n"
            f"\n"
            f"Write the 6-line analysis for the operator."
        )

        try:
            raw    = await self._llm_call(self._system_prompt, user_msg)
            logger.debug(f"[SentinelEvaluator] LLM raw response: {raw[:400]!r}")
            parsed    = _parse_structured_output(raw)
            reasoning = parsed.get("reasoning") or raw
            return reasoning, True, parsed

        except Exception as exc:
            logger.warning(
                f"[SentinelEvaluator] LLM enrichment failed "
                f"({type(exc).__name__}: {str(exc) or '(no detail)'}) — heuristic only"
            )
            return "", False, {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _heuristic_verdict(fired_checks: List[FiredCheck]) -> str:
        """
        Rule-based fallback when no LLM is available.

        Coercive-related checks → ALERT
        Any other check (structuring, scope, dimension) → WATCH
        No checks → CLEAR
        """
        if not fired_checks:
            return "CLEAR"
        coercive_checks = {
            "coercive_burst", "rapid_escalation", "high_coercive_ratio",
            "user_coercive_density", "dimension_concentration", "scope_violation",
            "agent_circular_dispute",
            # AML structuring patterns are ALERT-worthy.
            "permissive_tool_flood",
            # BSA §5324 cumulative dollar-amount structuring — always ALERT.
            "domestic_transfer_structuring",
        }
        for c in fired_checks:
            if c.name in coercive_checks:
                return "ALERT"
        return "WATCH"

    @staticmethod
    def _build_heuristic_reasoning(
        fired_checks:    List[FiredCheck],
        verdict:         str,
        derived_pattern: Optional[str] = None,
    ) -> str:
        """
        Synthesise a human-readable reasoning summary from fired pre-checks.

        Includes the deterministically derived pattern so the dashboard always
        shows meaningful signal even without LLM enrichment.
        """
        if not fired_checks:
            return "No anomalous patterns detected across pre-check metrics."
        severity     = "HIGH" if verdict == "ALERT" else "MEDIUM" if verdict == "WATCH" else "LOW"
        pattern_note = f"  Pattern: {derived_pattern}" if derived_pattern else ""
        lines = [
            f"Deterministic pre-check analysis — {len(fired_checks)} signal(s) "
            f"[severity: {severity}]{(' · ' + pattern_note.strip()) if pattern_note else ''}:\n",
        ]
        for c in fired_checks:
            lines.append(f"  • {c.name}  ({c.value:.3f} ≥ {c.threshold})")
            lines.append(f"    {c.description}")
        if not derived_pattern:
            lines.append(
                "\nNarrative enrichment pending LLM judge. "
                "Configure AZURE_OPENAI_* , ANTHROPIC_API_KEY or GROQ_API_KEY for operator narrative."
            )
        return "\n".join(lines)

    @staticmethod
    def _format_events(events: List[dict]) -> str:
        """
        Compact human-readable summary of events for the LLM prompt.

        Includes agent_name, tool_name, pii_detected, violations, violated
        dimension scores, and content_snippet (when STORE_CONTENT_SNIPPETS=true).
        """
        lines = []
        for i, e in enumerate(events, 1):
            violations      = e.get("violations") or []
            dims            = e.get("dimension_results") or []
            meta            = e.get("metadata") or {}
            content_snippet = (e.get("content_snippet") or "").strip()

            agent_name   = meta.get("agent_name", "")
            tool_name    = meta.get("tool_name",  "")
            pii_detected = meta.get("pii_detected")

            dim_summary = ", ".join(
                f"{d['dimension']}={d.get('score', '?')}"
                for d in dims
                if d.get("is_violation")
            )

            parts: List[str] = [
                f"  {i:2d}.",
                f"turn={str(e.get('turn_number', '?')):>3}",
                f"role={e.get('role', '?'):<15}",
                f"verdict={e.get('verdict', '?'):<12}",
            ]
            if agent_name:
                parts.append(f"agent={agent_name}")
            if tool_name:
                parts.append(f"tool={tool_name}")

            viol_str = "; ".join(violations[:3]) if violations else ""
            parts.append(f"violations=[{viol_str}]")

            if dim_summary:
                parts.append(f"dims=[{dim_summary}]")
            if pii_detected:
                parts.append("pii=True")

            lines.append("  ".join(parts))

            if content_snippet:
                snippet_display = content_snippet[:300]
                if len(content_snippet) > 300:
                    snippet_display += "…"
                lines.append(f'       content: "{snippet_display}"')

        return "\n".join(lines) if lines else "  (no events)"
