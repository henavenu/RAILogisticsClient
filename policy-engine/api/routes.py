"""
Policy Engine API routes.

Endpoints (SRS §4.5.7):
  POST /v1/app/evaluate     — primary evaluation endpoint
  GET  /v1/policies         — active policy catalog
  GET  /v1/health           — liveness/readiness; checks Guardrail Service and policy store
  GET  /v1/session-policy   — current session tracking policy (trust deltas, TTL, dedup)
  PUT  /v1/session-policy   — update session policy + hot-reload
  GET  /v1/audit/events     — recent audit events for a session/app (Sentinel pull)
"""
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from schemas.request  import PolicyEvaluateRequest
from schemas.response import PolicyEvaluateResponse, Directive
from engine.orchestrator import PolicyOrchestrator
from core.policy_loader  import PolicyRegistry
from session.store import save_session_policy, _build_policy_dict
from session import blocklist as _blocklist
from audit.reader import AuditReader

logger = logging.getLogger(__name__)


def create_router(
    orchestrator:    PolicyOrchestrator,
    policy_registry: PolicyRegistry,
    guardrail_url:   str,
    audit_reader:    Optional[AuditReader] = None,
) -> APIRouter:

    router = APIRouter()

    # ------------------------------------------------------------------ #
    #  POST /v1/app/evaluate                                              #
    # ------------------------------------------------------------------ #
    @router.post("/v1/app/evaluate", response_model=PolicyEvaluateResponse)
    def evaluate(request: PolicyEvaluateRequest):
        """
        Primary evaluation endpoint called by the RAI SDK.
        Orchestrates guardrail dispatch and MRA verdict synthesis.
        """
        try:
            return orchestrator.evaluate(request)
        except Exception as exc:
            logger.exception(
                f"Unhandled error in /v1/app/evaluate | "
                f"request_id={request.request_id} role={request.role.value}: {exc}"
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error":      "internal_evaluation_error",
                    "detail":     str(exc),
                    "request_id": request.request_id,
                },
            )

    # ------------------------------------------------------------------ #
    #  GET /v1/policies                                                   #
    # ------------------------------------------------------------------ #
    @router.get("/v1/policies")
    def get_policies():
        """Return the active policy catalog (thresholds, severity, remediations per dimension)."""
        catalog = policy_registry.as_catalog()
        return {
            "total":    len(catalog),
            "policies": catalog,
        }

    # ------------------------------------------------------------------ #
    #  GET /v1/health                                                     #
    # ------------------------------------------------------------------ #
    @router.get("/v1/health")
    def health():
        """
        Liveness/readiness check.
        Verifies reachability of:
          - Guardrail Service (GET /v1/health on Guardrail Service)
          - Policy store (policies.yaml loaded)
        Returns 503 if either is unhealthy.
        """
        checks = {}

        # Policy store
        policy_count = len(policy_registry.as_catalog())
        checks["policy_store"] = {
            "status":       "healthy" if policy_count > 0 else "degraded",
            "policy_count": policy_count,
        }

        # Guardrail Service
        try:
            r = httpx.get(f"{guardrail_url}/v1/health", timeout=3.0)
            gs_body = r.json()
            checks["guardrail_service"] = {
                "status":   gs_body.get("status", "unknown"),
                "url":      guardrail_url,
            }
        except Exception as e:
            checks["guardrail_service"] = {
                "status": "unreachable",
                "url":    guardrail_url,
                "error":  str(e),
            }

        all_healthy = all(c["status"] == "healthy" for c in checks.values())
        body = {
            "status": "healthy" if all_healthy else "degraded",
            "checks": checks,
        }
        return JSONResponse(
            status_code=200 if all_healthy else 503,
            content=body,
        )

    # ------------------------------------------------------------------ #
    #  GET /v1/session-policy                                              #
    # ------------------------------------------------------------------ #
    @router.get("/v1/session-policy")
    def get_session_policy():
        """Return the current live session tracking policy."""
        return _build_policy_dict()

    # ------------------------------------------------------------------ #
    #  PUT /v1/session-policy                                              #
    # ------------------------------------------------------------------ #
    @router.put("/v1/session-policy")
    def put_session_policy(body: dict):
        """
        Persist session policy to DB and hot-reload the in-process config.

        Accepts the same structure returned by GET /v1/session-policy.
        Unknown keys are silently ignored so partial updates are safe.
        """
        try:
            # Merge over defaults so partial payloads work
            current = _build_policy_dict()
            # top-level scalar fields
            for key in ("trust_floor", "trust_ceiling", "session_ttl_seconds"):
                if key in body:
                    current[key] = body[key]
            # trust_deltas sub-dict
            if "trust_deltas" in body:
                for verdict in ("COERCIVE", "NORMATIVE", "PERMISSIVE"):
                    if verdict in body["trust_deltas"]:
                        current["trust_deltas"][verdict] = body["trust_deltas"][verdict]
            # dedup sub-dict
            if "dedup" in body:
                for k in ("exact_block", "similarity_threshold", "window", "repetition_penalty"):
                    if k in body["dedup"]:
                        current["dedup"][k] = body["dedup"][k]

            save_session_policy(current)
            logger.info("[SessionPolicy] Hot-reloaded via PUT /v1/session-policy")
            return {"status": "ok", "config": _build_policy_dict()}
        except Exception as exc:
            logger.exception(f"[SessionPolicy] Failed to save: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    # ------------------------------------------------------------------ #
    #  POST /v1/sessions/agent-block                                      #
    # ------------------------------------------------------------------ #
    @router.post("/v1/sessions/agent-block", status_code=200)
    def agent_block(body: dict):
        """
        Block a specific agent and/or tool within a session.

        Called by the Sentinel Agent when it detects an ALERT-level pattern
        (e.g. BSA §5324 structuring).  Subsequent evaluate() calls for the
        blocked agent/tool in this session return COERCIVE immediately.

        Body fields (all optional except session_id):
          session_id  — required
          agent_name  — name of the agent to block (e.g. "domestic_agent")
          tool_name   — name of the tool to block   (e.g. "domestic_transfer")
          reason      — human-readable reason (logged, included in violations)
          pattern     — sentinel pattern type (e.g. "structuring")
          blocked_by  — who triggered the block (default "sentinel")
        """
        session_id = body.get("session_id", "")
        if not session_id:
            raise HTTPException(status_code=422, detail="session_id is required")

        agent_name = body.get("agent_name", "")
        tool_name  = body.get("tool_name",  "")
        reason     = body.get("reason",     "sentinel_block")
        blocked_by = body.get("blocked_by", "sentinel")

        if not agent_name and not tool_name:
            raise HTTPException(
                status_code=422,
                detail="At least one of 'agent_name' or 'tool_name' is required",
            )

        if agent_name:
            _blocklist.block_agent(session_id, agent_name, reason=reason, blocked_by=blocked_by)
        if tool_name:
            _blocklist.block_tool(session_id, tool_name, reason=reason, blocked_by=blocked_by)

        logger.info(
            f"[agent-block] session={session_id} "
            f"agent={agent_name or '-'} tool={tool_name or '-'} "
            f"reason={reason!r} by={blocked_by}"
        )
        return {
            "status":   "blocked",
            "session_id": session_id,
            "blocks":   _blocklist.get_session_blocks(session_id),
        }

    # ------------------------------------------------------------------ #
    #  DELETE /v1/sessions/agent-block                                   #
    # ------------------------------------------------------------------ #
    @router.delete("/v1/sessions/agent-block", status_code=200)
    def clear_all_agent_blocks():
        """
        Clear all session-scoped agent/tool blocks.

        Called by the demo reset endpoint so Story 1 (structuring) can restart
        cleanly with the same browser SESSION_ID.  Not for production use.
        """
        _blocklist.clear_all_blocks()
        return {"cleared": True}

    # ------------------------------------------------------------------ #
    #  POST /internal/session-freeze  — trip/clear circuit-breaker        #
    #  GET  /internal/session-risk/{session_id} — read risk record        #
    # ------------------------------------------------------------------ #
    @router.post("/internal/session-freeze", status_code=200)
    def set_session_freeze(body: dict):
        """
        Trip or clear the per-session circuit-breaker freeze (Phase 1).
        Body: {session_id, app_id, frozen: bool, reason?: str, ttl?: float}.
        While frozen, the PE gate returns COERCIVE for every action in the session.
        Called by tests now; by the async detector fleet (Phase 2) on a CONFIRMED runaway.
        """
        from fastapi import HTTPException
        session_id = body.get("session_id", "")
        app_id     = body.get("app_id", "")
        if not session_id or not app_id:
            raise HTTPException(status_code=422, detail="session_id and app_id are required")
        res = orchestrator.set_session_freeze(
            session_id, app_id, bool(body.get("frozen", True)),
            reason=body.get("reason", ""), ttl=float(body.get("ttl", 0) or 0),
        )
        return res or {"session_id": session_id, "app_id": app_id, "frozen": False, "note": "no session store"}

    @router.get("/internal/session-risk/{session_id}", status_code=200)
    def get_session_risk(session_id: str, app_id: str = "mybank"):
        """Read the standing per-session emergent-risk record (verdict, freeze, accumulators)."""
        res = orchestrator.get_session_risk(session_id, app_id)
        return res or {"session_id": session_id, "app_id": app_id, "found": False}

    @router.get("/internal/flagged-sessions", status_code=200)
    def get_flagged_sessions(app_id: str = "mybank"):
        """
        Risk-store-driven discovery for the async emergent detector (Phase 2).
        Returns every live session the deterministic gate flagged WATCH/CONFIRMED and
        not yet frozen, with its compact payment stream. This is the detector's
        discovery surface — driven by the risk store, NOT the audit firehose (which
        misses /v1/app/evaluate sessions).
        """
        return {"app_id": app_id, "sessions": orchestrator.get_flagged_sessions(app_id)}

    @router.post("/internal/session-verdict", status_code=200)
    def set_session_verdict(body: dict):
        """
        Set the standing emergent-risk verdict for a session (CLEAR|WATCH|CONFIRMED).
        Body: {session_id, app_id, verdict, reason?}. Called by the async detector fleet
        (Phase 2) to record its judgment — e.g. CLEAR to release a deterministic false
        positive, or CONFIRMED alongside a freeze.
        """
        from fastapi import HTTPException
        session_id = body.get("session_id", "")
        app_id     = body.get("app_id", "")
        verdict    = str(body.get("verdict", "")).upper()
        if not session_id or not app_id or verdict not in ("CLEAR", "WATCH", "CONFIRMED"):
            raise HTTPException(status_code=422, detail="session_id, app_id, verdict(CLEAR|WATCH|CONFIRMED) required")
        res = orchestrator.set_session_verdict(session_id, app_id, verdict, body.get("reason", ""),
                                               risk_class=str(body.get("risk_class", "")))
        return res or {"session_id": session_id, "app_id": app_id, "note": "no session store"}

    # ------------------------------------------------------------------ #
    #  POST /v1/session/{session_id}/directive                            #
    # ------------------------------------------------------------------ #
    @router.post("/v1/session/{session_id}/directive", status_code=202)
    def post_session_directive(session_id: str, directive: Directive):
        """
        Queue a governance directive for a session.

        Called by the Sentinel Agent when a cross-turn pattern threshold is
        exceeded (e.g. same agent pair back-and-forth ≥ 3 rounds).
        The directive is attached to the next evaluate() response for the
        session so the SDK can raise HITLInterrupt with the full context.

        Returns 202 Accepted immediately — the directive is applied
        asynchronously on the SDK's next evaluation call.

        Body: a Directive JSON object (action, category, reason, options,
              source, metadata).
        """
        if not session_id:
            raise HTTPException(status_code=422, detail="session_id must not be empty")
        try:
            orchestrator.receive_directive(session_id, directive)
        except Exception as exc:
            logger.exception(f"[directive] Failed to queue for session={session_id}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
        logger.info(
            f"[directive] Accepted — session={session_id} "
            f"action={directive.action.value} source={directive.source.value}"
        )
        return {
            "status":     "accepted",
            "session_id": session_id,
            "action":     directive.action.value,
            "source":     directive.source.value,
        }

    # ------------------------------------------------------------------ #
    #  GET /v1/audit/events                                               #
    # ------------------------------------------------------------------ #
    @router.get("/v1/audit/events")
    def get_audit_events(
        session_id:   Optional[str] = Query(default=None,  description="Filter by session ID"),
        app_id:       Optional[str] = Query(default=None,  description="Filter by application ID"),
        role:         Optional[str] = Query(default=None,  description="Filter by content role (user_prompt, llm_response, tool_call, …)"),
        limit:        int           = Query(default=20,    ge=1, le=200, description="Maximum events to return (newest first)"),
        include_dims: bool          = Query(default=True,  description="Attach per-dimension scores to each event"),
    ):
        """
        Fetch recent audit events for a session or application (newest first).

        Designed for the Sentinel Agent's pull model: Sentinel calls this
        endpoint after receiving a webhook trigger and then computes cross-turn
        aggregates (structuring, salami, session-level patterns) locally.

        At least one of ``session_id`` or ``app_id`` is required.
        Results are ordered by ``turn_number DESC, timestamp DESC``.
        """
        if audit_reader is None:
            raise HTTPException(
                status_code=503,
                detail="Audit reader not configured — set AUDIT_SQLITE_PATH to enable this endpoint.",
            )

        if not session_id and not app_id:
            raise HTTPException(
                status_code=422,
                detail="At least one of 'session_id' or 'app_id' query parameters is required.",
            )

        try:
            events = audit_reader.get_events(
                session_id=session_id,
                app_id=app_id,
                role=role,
                limit=limit,
                include_dims=include_dims,
            )
            return {
                "session_id": session_id,
                "app_id":     app_id,
                "total":      len(events),
                "limit":      limit,
                "events":     events,
            }
        except Exception as exc:
            logger.exception(f"[GET /v1/audit/events] Unexpected error: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    return router
