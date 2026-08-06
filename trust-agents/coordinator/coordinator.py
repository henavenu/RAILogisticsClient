"""
CoordinatorEngine — tiered intervention pipeline for the Coordinator Agent.

Architecture — Fleet of Dimension-Specialising Monitoring Sub-Agents
────────────────────────────────────────────────────────────────────
The Coordinator is a fleet of monitoring sub-agents, each specialising in a
specific governance dimension or behavioural pattern (AML structuring, sanctions
evasion, scope enforcement, agent conflict arbitration, etc.).

When the Sentinel issues an ALERT, the Coordinator delegates analysis to the
appropriate dimension-specialist sub-agent and synthesises their findings into
tiered, actionable interventions — producing advisory actions targeted at the
exact violation pattern, RAI pillar, and agent(s) implicated.

Layer responsibilities
──────────────────────
  Sentinel    DETECTS cross-turn patterns (ALERT / WATCH / CLEAR)
  Coordinator RECOMMENDS tiered interventions (never issues verdicts)

Four intervention tiers
───────────────────────
  Tier 1 — HITL           Human-in-the-loop; operator suspension, account lock
  Tier 2 — ALERT_NOTIFY   Automated alerts; SAR filing, stakeholder notification
  Tier 3 — POLICY_CHANGE  Governance rule or threshold modification — Policy Review
  Tier 4 — AGENT_CHANGE   Agent prompt or tool-scope revision request

Each intervention carries an ITSM artifact annotation (INCIDENT / CHANGE_REQUEST /
PROBLEM / NONE) with ServiceNow field hints so operators can create tickets directly.

LLM provider priority: Google Vertex AI → Azure OpenAI → Groq → Anthropic → Ollama
→ heuristic fallback. (Ollama is local-dev only.)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from schemas import CoordinatorReport, CoordinatorRequest, Intervention
from store   import ReportStore

logger = logging.getLogger(__name__)

# ── LLM factories ─────────────────────────────────────────────────────────────

def _try_anthropic(model: str, max_tokens: int):
    try:
        import anthropic, os
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return None
        client = anthropic.AsyncAnthropic(api_key=key)
        logger.info(f"[Coordinator] LLM provider: Anthropic ({model})")

        async def call(system: str, user: str) -> str:
            resp = await client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
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
        logger.info(f"[Coordinator] LLM provider: Azure OpenAI ({deployment})")

        async def call(system: str, user: str) -> str:
            resp = await client.chat.completions.create(
                model=deployment, max_tokens=max_tokens,
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
        logger.info(f"[Coordinator] LLM provider: Groq ({model})")

        async def call(system: str, user: str) -> str:
            resp = await client.chat.completions.create(
                model=model, max_tokens=max_tokens,
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
        logger.info(f"[Coordinator] LLM provider: Google Gemini ({model}) — Vertex/{location}")

        async def call(system: str, user: str) -> str:
            resp = await llm.ainvoke([SystemMessage(content=system),
                                      HumanMessage(content=user)])
            c = resp.content
            return (c if isinstance(c, str) else str(c)).strip()
        return call
    except ImportError:
        return None
    except Exception as exc:
        logger.warning(f"[Coordinator] Vertex AI init failed ({exc}) — skipping")
        return None


def _try_ollama(base_url: str, model: str, max_tokens: int):
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return None
    logger.info(f"[Coordinator] LLM provider: Ollama ({model} @ {base_url})")

    async def call(system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model":      model,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    return call


# ── Engine ─────────────────────────────────────────────────────────────────────

class CoordinatorEngine:
    """
    Orchestrates the tiered intervention recommendation pipeline.

    Parameters
    ----------
    policy        Parsed policy.yaml dict.
    sentinel_url  Base URL of the Sentinel service (e.g. http://localhost:3012).
    store         ReportStore instance to persist results.
    http_timeout  Timeout for Sentinel API calls (seconds).
    pap_url       Base URL of the PAP service for governance alert creation.
                  When set, the Coordinator writes an INCIDENT governance alert
                  to PAP after each ALERT-verdict coordination.
    """

    def __init__(
        self,
        policy:       Dict[str, Any],
        sentinel_url: str,
        store:        ReportStore,
        http_timeout: float = 5.0,
        pap_url:      Optional[str] = None,
    ) -> None:
        self._policy       = policy
        self._sentinel_url = sentinel_url.rstrip("/")
        self._store        = store
        self._http_timeout = http_timeout
        self._pap_url      = pap_url.rstrip("/") if pap_url else None

        import os as _os
        judge_cfg        = policy.get("llm_judge", {})
        _max_tokens      = int(judge_cfg.get("max_tokens", 1000))
        _anthropic_model = _os.getenv("ANTHROPIC_MODEL",
                           judge_cfg.get("anthropic_model", "claude-sonnet-4-6"))

        # Provider priority: Google Vertex (Gemini) → Anthropic → Azure → Groq → heuristic.
        # CLOUD uses Gemini (GOOGLE_CLOUD_PROJECT) or Anthropic; LOCAL uses Groq or
        # Anthropic. Gemini is first so GCP/Cloud Run selects it; to force Anthropic,
        # set ANTHROPIC_API_KEY and leave GOOGLE_CLOUD_PROJECT unset. Each factory
        # returns None when its config is absent; if none match, heuristic
        # interventions are used.
        _gemini_model = _os.getenv("GEMINI_MODEL", judge_cfg.get("gemini_model", "gemini-2.5-flash"))
        self._llm_call = (
            _try_vertex(_gemini_model, _max_tokens)
            or _try_anthropic(_anthropic_model, _max_tokens)
            or _try_azure_openai(_max_tokens)
            or _try_groq(judge_cfg.get("groq_model", "llama-3.1-8b-instant"),
                         _max_tokens)
        )
        if self._llm_call is None:
            logger.warning(
                "[Coordinator] No LLM available. "
                "Heuristic interventions will be used."
            )

        self._system_prompt = judge_cfg.get("system_prompt", "")
        logger.info(
            f"[Coordinator] Ready — "
            f"Sentinel: {self._sentinel_url}  llm_active: {self._llm_call is not None}"
        )

    # ── Public ─────────────────────────────────────────────────────────────────

    async def coordinate(self, request: CoordinatorRequest) -> CoordinatorReport:
        """
        Run the full coordination pipeline and persist the report.

        Steps:
          1. Fetch Sentinel verdict (or use caller-supplied fields)
          2. LLM / heuristic → typed interventions
          3. Persist CoordinatorReport
        """
        session_id = request.session_id

        # ── Step 1: Get Sentinel verdict ───────────────────────────────────────
        sentinel_verdict   = request.sentinel_verdict
        sentinel_checks    = request.sentinel_checks
        sentinel_reasoning = request.sentinel_reasoning
        pattern_type       = request.pattern_type
        rai_pillar         = request.rai_pillar
        rai_dimensions     = request.rai_dimensions
        agents_implicated  = request.agents_implicated
        confidence         = request.confidence

        if sentinel_verdict is None:
            sv = await self._fetch_sentinel_verdict(session_id)
            if sv:
                sentinel_verdict  = sv.get("verdict", "CLEAR")
                sentinel_checks   = [c.get("name", "") for c in (sv.get("fired_checks") or [])]
                sentinel_reasoning = sv.get("llm_reasoning", "")
                pattern_type      = sv.get("pattern_type")
                rai_pillar        = sv.get("rai_pillar")
                rai_dimensions    = sv.get("rai_dimensions") or []
                agents_implicated = sv.get("agents_implicated") or []
                confidence        = sv.get("confidence")
            else:
                sentinel_verdict = "CLEAR"

        logger.info(
            f"[Coordinator] Coordinating — "
            f"session={session_id}  sentinel={sentinel_verdict}  "
            f"pattern={pattern_type or '-'}  pillar={rai_pillar or '-'}  "
            f"checks={sentinel_checks}"
        )

        # ── Step 2: Generate interventions ─────────────────────────────────────
        interventions, summary, llm_used = await self._generate_interventions(
            sentinel_verdict   = sentinel_verdict,
            sentinel_checks    = sentinel_checks,
            sentinel_reasoning = sentinel_reasoning,
            pattern_type       = pattern_type,
            rai_pillar         = rai_pillar,
            rai_dimensions     = rai_dimensions,
            agents_implicated  = agents_implicated,
            confidence         = confidence,
            session_id         = session_id,
            app_id             = request.app_id,
        )

        # ── Step 3: Overall risk mapping ────────────────────────────────────────
        risk_map     = self._policy.get("risk_map", {})
        overall_risk = risk_map.get(sentinel_verdict, "LOW")

        report = CoordinatorReport(
            session_id        = session_id,
            app_id            = request.app_id,
            sentinel_verdict  = sentinel_verdict,
            overall_risk      = overall_risk,
            pattern_type      = pattern_type,
            rai_pillar        = rai_pillar,
            interventions     = interventions,
            summary           = summary,
            llm_used          = llm_used,
        )

        self._store.put(report)
        logger.info(
            f"[Coordinator] Report ready — "
            f"session={session_id}  risk={overall_risk}  "
            f"interventions={len(interventions)}  llm={llm_used}"
        )

        # ── Step 4: Write INCIDENT governance alert to PAP ─────────────────────
        # Only fires when PAP URL is configured and Sentinel verdict is ALERT.
        # Non-fatal — a failed PAP write never blocks the coordination report.
        if self._pap_url and sentinel_verdict == "ALERT" and interventions:
            await self._write_pap_incident_alert(report, session_id)

        return report

    async def _write_pap_incident_alert(
        self,
        report:     "CoordinatorReport",
        session_id: str,
    ) -> None:
        """
        POST an INCIDENT governance alert to PAP's internal endpoint.
        Non-blocking best-effort — errors are logged but never re-raised.
        """
        tier1_ivs = [iv for iv in report.interventions if iv.tier == 1]
        top_iv    = tier1_ivs[0] if tier1_ivs else (report.interventions[0] if report.interventions else None)

        payload = {
            "tier":         "INCIDENT",
            "severity":     "URGENT" if report.overall_risk == "HIGH" else "HIGH",
            "raiser":       "coordinator",
            "raiser_detail": f"coordinator:sentinel_{report.sentinel_verdict}",
            "caller_type":  "session",
            "caller_id":    session_id,
            "app_id":       report.app_id,
            "reason_code":  "sentinel_alert",
            "reason_text":  report.summary or f"Sentinel ALERT: {report.pattern_type}",
            "condition":    f"sentinel_verdict=ALERT risk={report.overall_risk} pattern={report.pattern_type}",
            "session_id":   session_id,
            "coord_action": top_iv.action if top_iv else None,
            "coord_target": top_iv.target if top_iv else None,
            "coord_itsm":   top_iv.itsm_fields if top_iv else None,
            "payload": {
                "sentinel_verdict":  report.sentinel_verdict,
                "pattern_type":      report.pattern_type,
                "rai_pillar":        report.rai_pillar,
                "overall_risk":      report.overall_risk,
                "interventions":     len(report.interventions),
                "llm_used":          report.llm_used,
                "coordinated_at":    report.coordinated_at,
            },
        }

        url = f"{self._pap_url}/internal/governance/alerts"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.post(url, json=payload)
            if resp.is_success:
                alert_id = resp.json().get("id", "?")
                logger.info(
                    f"[Coordinator] INCIDENT governance alert written — "
                    f"id={alert_id}  session={session_id}  risk={report.overall_risk}"
                )
            else:
                logger.warning(
                    f"[Coordinator] PAP alert write non-2xx {resp.status_code} — "
                    f"session={session_id}: {resp.text[:200]}"
                )
        except Exception as exc:
            logger.error(
                f"[Coordinator] PAP alert write failed for session={session_id}: {exc}"
            )

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _fetch_sentinel_verdict(self, session_id: str) -> Optional[dict]:
        """Pull the latest Sentinel verdict for this session. Returns None on failure."""
        url = f"{self._sentinel_url}/verdicts/{session_id}"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url)
            if resp.is_success:
                data = resp.json()
                return data.get("verdict") if data.get("found") else None
            logger.warning(f"[Coordinator] Sentinel returned {resp.status_code} for {url}")
            return None
        except Exception as exc:
            logger.warning(f"[Coordinator] Failed to fetch Sentinel verdict: {exc}")
            return None

    def _build_context_block(
        self,
        *,
        sentinel_checks:   List[str],
        pattern_type:      Optional[str],
        rai_pillar:        Optional[str],
        rai_dimensions:    List[str],
        agents_implicated: List[str],
        confidence:        Optional[float],
    ) -> str:
        """Build an enriched context block for the LLM user message."""
        lines = []
        if pattern_type:
            lines.append(f"Behavioural pattern detected: {pattern_type}")
        if rai_pillar:
            lines.append(f"Primary RAI pillar affected: {rai_pillar}")
        if rai_dimensions:
            lines.append(f"PE dimensions involved: {', '.join(rai_dimensions)}")
        if agents_implicated:
            lines.append(f"Agents implicated: {', '.join(agents_implicated)}")
        if confidence is not None:
            lines.append(f"Classification confidence: {confidence:.2f}")
        if sentinel_checks:
            check_desc = self._policy.get("check_descriptions", {})
            check_lines = []
            for name in sentinel_checks:
                desc = check_desc.get(name, "")
                check_lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
            lines.append("Pre-checks that fired:\n" + "\n".join(check_lines))
        return "\n".join(lines) if lines else "No enriched context available."

    async def _generate_interventions(
        self,
        *,
        sentinel_verdict:   str,
        sentinel_checks:    List[str],
        sentinel_reasoning: str,
        pattern_type:       Optional[str],
        rai_pillar:         Optional[str],
        rai_dimensions:     List[str],
        agents_implicated:  List[str],
        confidence:         Optional[float],
        session_id:         str,
        app_id:             Optional[str],
    ) -> Tuple[List[Intervention], str, bool]:
        """Return (interventions, summary, llm_used)."""
        if self._llm_call is None:
            return (
                self._heuristic(sentinel_verdict, sentinel_checks, pattern_type),
                self._heuristic_summary(sentinel_verdict, sentinel_checks, pattern_type),
                False,
            )

        context_block = self._build_context_block(
            sentinel_checks   = sentinel_checks,
            pattern_type      = pattern_type,
            rai_pillar        = rai_pillar,
            rai_dimensions    = rai_dimensions,
            agents_implicated = agents_implicated,
            confidence        = confidence,
        )
        user_msg = (
            f"Session: {session_id}\n"
            f"App: {app_id or 'unknown'}\n"
            f"Sentinel verdict: {sentinel_verdict}\n"
            f"\n{context_block}\n"
            f"\nSentinel reasoning: {sentinel_reasoning or 'not available'}\n"
            f"\nProduce 2–4 typed interventions across appropriate tiers."
        )

        try:
            raw  = await self._llm_call(self._system_prompt, user_msg)
            data = self._parse_llm_output(raw)
            if data:
                interventions = []
                for item in (data.get("interventions") or [])[:4]:
                    try:
                        interventions.append(Intervention(
                            tier          = int(item.get("tier", 2)),
                            tier_label    = item.get("tier_label", "ALERT_NOTIFY"),
                            priority      = item.get("priority",   "MEDIUM"),
                            action        = item.get("action",     "Review session"),
                            rationale     = item.get("rationale",  ""),
                            target        = item.get("target",     "OPERATOR"),
                            itsm_artifact = item.get("itsm_artifact", "NONE"),
                            itsm_fields   = item.get("itsm_fields",  {}),
                        ))
                    except Exception as parse_exc:
                        logger.debug(f"[Coordinator] Skipping malformed intervention: {parse_exc}")

                summary = data.get("summary", "")
                logger.debug(
                    f"[Coordinator] LLM produced {len(interventions)} interventions "
                    f"for {sentinel_verdict} verdict"
                )
                return interventions, summary, True

        except Exception as exc:
            logger.warning(f"[Coordinator] LLM call failed ({exc}) — using heuristic")

        return (
            self._heuristic(sentinel_verdict, sentinel_checks, pattern_type),
            self._heuristic_summary(sentinel_verdict, sentinel_checks, pattern_type),
            False,
        )

    # ── JSON parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_llm_output(text: str) -> Optional[dict]:
        """Extract JSON from LLM output, handling markdown fences and truncation."""
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            candidate = m.group(0)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                repaired = _repair_truncated_json(candidate)
                if repaired:
                    try:
                        return json.loads(repaired)
                    except json.JSONDecodeError:
                        pass
        logger.debug("[Coordinator] _parse_llm_output: could not parse LLM response")
        return None

    # ── Heuristic fallback ────────────────────────────────────────────────────

    def _heuristic(
        self,
        verdict:      str,
        checks:       List[str],
        pattern_type: Optional[str] = None,
    ) -> List[Intervention]:
        """
        Return check-aware heuristic interventions from policy.yaml.

        Priority:
          1. pattern_type  — highest specificity; allows compound patterns to
                             map directly to the right intervention without
                             relying on which individual check fired first.
          2. fired check   — most-specific fired check name.
          3. _default      — verdict-level fallback.
        """
        heuristic_block = self._policy.get("heuristic_interventions", {})
        verdict_block   = heuristic_block.get(verdict, {})

        if verdict == "CLEAR":
            return []

        # Priority 1: pattern_type (e.g. "agent_conflict", "structuring")
        if pattern_type:
            pattern_items = verdict_block.get(pattern_type)
            if pattern_items is not None:
                return [_dict_to_intervention(item) for item in pattern_items[:4]]

        # Priority 2: most-specific fired check
        for check in checks:
            check_items = verdict_block.get(check)
            if check_items is not None:
                return [_dict_to_intervention(item) for item in check_items[:4]]

        # Priority 3: verdict-level default
        default_items = verdict_block.get("_default", [])
        return [_dict_to_intervention(item) for item in default_items[:4]]

    @staticmethod
    def _heuristic_summary(
        verdict: str, checks: List[str], pattern_type: Optional[str]
    ) -> str:
        check_str   = ", ".join(checks) if checks else "no specific check"
        pattern_str = f" ({pattern_type})" if pattern_type else ""
        summaries = {
            "ALERT": (
                f"Sentinel flagged this session as HIGH risk{pattern_str} "
                f"(checks fired: {check_str}). "
                "Tiered interventions below require immediate operator action and "
                "compliance review."
            ),
            "WATCH": (
                f"Sentinel flagged this session for monitoring{pattern_str} "
                f"(checks fired: {check_str}). "
                "Enhanced scrutiny and policy calibration recommended."
            ),
            "CLEAR": (
                "No significant cross-turn pattern detected. "
                "Session is within normal governance parameters."
            ),
        }
        return summaries.get(verdict, "Coordination complete.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dict_to_intervention(item: dict) -> Intervention:
    """Convert a heuristic_interventions YAML entry to an Intervention model."""
    return Intervention(
        tier          = int(item.get("tier", 2)),
        tier_label    = item.get("tier_label", "ALERT_NOTIFY"),
        priority      = item.get("priority",   "MEDIUM"),
        action        = item.get("action",     "Review session"),
        rationale     = item.get("rationale",  ""),
        target        = item.get("target",     "OPERATOR"),
        itsm_artifact = item.get("itsm_artifact", "NONE"),
        itsm_fields   = item.get("itsm_fields",   {}),
    )


def _repair_truncated_json(text: str) -> Optional[str]:
    """Best-effort repair of JSON truncated mid-stream (e.g. phi3:mini token cutoff)."""
    try:
        depth_brace = depth_bracket = 0
        in_string = escape = False
        for ch in text:
            if escape:
                escape = False; continue
            if ch == "\\" and in_string:
                escape = True; continue
            if ch == '"':
                in_string = not in_string; continue
            if in_string:
                continue
            if ch == "{":   depth_brace   += 1
            elif ch == "}": depth_brace   -= 1
            elif ch == "[": depth_bracket += 1
            elif ch == "]": depth_bracket -= 1

        if depth_brace <= 0 and depth_bracket <= 0:
            return None

        trimmed = re.sub(r",\s*$", "", text.rstrip())
        closing = ("]" * max(depth_bracket, 0)) + ("}" * max(depth_brace, 0))
        return trimmed + closing
    except Exception:
        return None
