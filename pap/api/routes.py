"""
PAP API routes.

Policy CRUD:
  GET    /v1/policies                → list all policies
  POST   /v1/policies                → create policy
  GET    /v1/policies/{dimension}    → get single policy
  PUT    /v1/policies/{dimension}    → update policy
  DELETE /v1/policies/{dimension}    → soft-delete

Publishing:
  POST   /v1/policies/publish        → write policies.yaml + hot-reload PE

App Configs:
  GET    /v1/app-configs             → list app configs
  POST   /v1/app-configs             → upsert app config
  DELETE /v1/app-configs/{app_id}    → delete app config

Audit — Policy Admin:
  GET    /v1/audit                   → recent policy-change audit log entries

Audit — Evaluations (Phase 3):
  GET    /v1/audit/evaluations       → recent evaluation records from PE audit.jsonl

Proxy (for UI):
  GET    /v1/proxy/evaluations       → forwards to Guardrail Service
  GET    /v1/proxy/services          → health of GS + PE

Health:
  GET    /v1/health                  → PAP health check
"""
import asyncio
import json
import logging
import httpx
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database  import get_db
from core.models    import Policy, App, PapAuditLog, EvaluationDimension, Provider, AppAgent, ServiceConfig, AppTrustWeights
from core.policy_sync import publish as _publish
from schemas.request import PolicyCreate, PolicyUpdate, PublishRequest, AppUpsert, EvaluatorPolicyUpsert, EvaluatorRolesUpdate

import os
import sys

def _require_env(name: str, why: str = "") -> str:
    """Peer-service URLs have NO silent fallback. A missing one aborts startup
    with a clear error instead of defaulting to a dead localhost port. (The only
    legitimate fallback in this platform is the LLM provider chain.)"""
    v = os.getenv(name, "").strip()
    if not v:
        sys.stderr.write(
            f"\nFATAL [PAP]: required env '{name}' is not set"
            + (f" — {why}" if why else "")
            + ".\nRefusing to start (there is no localhost fallback). "
              "Set it in the environment / .env.\n\n"
        )
        raise SystemExit(1)
    return v

GUARDRAIL_URL     = _require_env("GUARDRAIL_SERVICE_URL", "PAP reads provider config / triggers Guardrail reload")
POLICY_ENGINE_URL = _require_env("POLICY_ENGINE_URL",     "PAP publishes & syncs policies to the Policy Engine")
SENTINEL_URL      = _require_env("SENTINEL_URL",          "PAP reads Trust Radar findings from the Sentinel")
COORDINATOR_URL   = _require_env("COORDINATOR_URL",       "PAP reads Coordinator reports")

logger = logging.getLogger(__name__)

# ── In-memory HITL queue (demo — cleared on PAP restart) ─────────────────────
# session_id → {session_id, reason, violations, agent, tool, app_id,
#               callback_url, received_at, status, decision?, reviewer?, resolved_at?}
_hitl_items: Dict[str, dict] = {}


async def _write_hitl_audit_to_pe(
    session_id: str,
    app_id:     str,
    event_type: str,          # "hitl_initiated" | "hitl_resolved"
    decision:   str,          # "pending" | "approved" | "rejected"
    reviewer:   str,
    agent:      str,
    tool:       str,
) -> None:
    """
    Write a HITL lifecycle audit record to the PE using the hitl_checkpoint role.

    The PE passes hitl_checkpoint events through without guardrail evaluation —
    it writes the audit row and returns PERMISSIVE immediately.  This ensures
    HITL events appear in the evaluation trail (for audit completeness) but do
    NOT create extra turn entries, affect trust scores, or fire governance blocks.

    Fire-and-forget — called via asyncio.create_task; failures are logged only.
    """
    import uuid as _uuid
    verb = (
        f"HITL review initiated — awaiting compliance decision"
        if event_type == "hitl_initiated"
        else f"HITL review resolved — reviewer decision: {decision}"
    )
    content = (
        f"Governance checkpoint: {verb}. "
        f"Agent: {agent or 'n/a'}. Tool: {tool or 'n/a'}. "
        f"Session {session_id}. Recorded by PAP."
    )
    payload = {
        "content":    content,
        "role":       "hitl_checkpoint",
        "request_id": str(_uuid.uuid4()),
        "session_id": session_id,
        "app_id":     app_id,
        "metadata": {
            "event_type": event_type,
            "decision":   decision,
            "reviewer":   reviewer,
            "agent_name": agent,   # must be "agent_name" for _derive_edge() to pick it up
            "tool":       tool,
            "source":     "pap_hitl",
            # Parity with MyBank's removed initiation write (fix #1): stamp NORMATIVE
            # on the OPENING checkpoint so the interaction diagram renders the amber
            # NORMATIVE HITL node. The PE pass-through honours metadata.governance_verdict
            # (see policy-engine/engine/orchestrator.py). Resolution rows keep their own.
            **({"governance_verdict": "NORMATIVE"} if event_type == "hitl_initiated" else {}),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{POLICY_ENGINE_URL}/v1/app/evaluate",
                json=payload,
            )
        if r.is_success:
            logger.info(
                "[PAP] HITL audit event written to PE — session=%s type=%s",
                session_id, event_type,
            )
        else:
            logger.warning(
                "[PAP] PE returned %s for HITL audit event (session=%s type=%s)",
                r.status_code, session_id, event_type,
            )
    except Exception as exc:
        logger.warning(
            "[PAP] Failed to write HITL audit event to PE — session=%s type=%s: %s",
            session_id, event_type, exc,
        )


# ── Module-level metric helpers ───────────────────────────────────────────────
# Defined here (not inside tier1_metrics) so they can never be shadowed by a
# loop variable or local assignment inside the endpoint function body.

def _metrics_avg(lst) -> Optional[float]:
    """Mean of a list, rounded to 3 dp.  Returns None on empty list."""
    return round(sum(lst) / len(lst), 3) if lst else None


def _metrics_p95(lst) -> Optional[float]:
    """95th-percentile of a list, rounded to 1 dp.  Returns None on empty list."""
    import math
    if not lst:
        return None
    s   = sorted(lst)
    idx = max(0, math.ceil(len(s) * 0.95) - 1)
    return round(s[idx], 1)


def _get_actor(x_actor: Optional[str] = Header(None)) -> str:
    return x_actor or "pap-admin"


class PiiScanRequest(BaseModel):
    text: str


class AgentCardFlagsUpdate(BaseModel):
    # Governance config flags — Optional so a PATCH can set either independently.
    kill_switch:     Optional[bool] = None
    hitl_mode:       Optional[str]  = None   # none | by_design | agent | human | agent_human


def create_router() -> APIRouter:
    router = APIRouter()

    # ================================================================== #
    #  POLICIES                                                           #
    # ================================================================== #

    @router.get("/v1/policies")
    def list_policies(
        include_inactive: bool = Query(False),
        app_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
    ):
        q = db.query(Policy)
        if not include_inactive:
            q = q.filter(Policy.active == True)
        if app_id is not None:
            q = q.filter(Policy.app_id == app_id)
        else:
            q = q.filter(Policy.app_id == None)
        policies = q.order_by(Policy.dimension).all()
        return {
            "total":    len(policies),
            "policies": [p.to_dict() for p in policies],
        }

    @router.post("/v1/policies", status_code=201)
    def create_policy(
        body: PolicyCreate,
        db:   Session = Depends(get_db),
        actor: str    = Depends(_get_actor),
    ):
        # Check for duplicate
        existing = db.query(Policy).filter(
            Policy.dimension == body.dimension,
            Policy.app_id    == body.app_id,
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Policy for dimension '{body.dimension}' "
                       f"(app_id={body.app_id!r}) already exists. Use PUT to update.",
            )

        t = body.thresholds
        p = Policy(
            dimension=body.dimension,
            app_id=body.app_id,
            severity=body.severity,
            permissive_lo=t.permissive[0], permissive_hi=t.permissive[1],
            normative_lo=t.normative[0],   normative_hi=t.normative[1],
            coercive_lo=t.coercive[0],     coercive_hi=t.coercive[1],
            remediation_normative=body.remediation_normative,
            remediation_coercive= body.remediation_coercive,
            policy_code=body.policy_code,
            normative_action=body.normative_action,
            normative_guidance=body.normative_guidance,
            human_note=body.human_note,
            ops_alert=body.ops_alert,
            hitl_trigger=body.hitl_trigger,
            incident_routing=body.incident_routing,
        )
        db.add(p)
        db.flush()

        db.add(PapAuditLog(
            entity_type="policy", entity_id=p.dimension, app_id=p.app_id,
            action="CREATE", actor=actor,
            new_values=json.dumps(p.to_dict()),
        ))
        db.commit()
        db.refresh(p)
        logger.info(f"[PAP] CREATE policy={p.dimension} app_id={p.app_id} actor={actor}")
        return JSONResponse(status_code=201, content=p.to_dict())

    @router.get("/v1/policies/{dimension}")
    def get_policy(
        dimension: str,
        app_id:    Optional[str] = Query(None),
        db:        Session = Depends(get_db),
    ):
        p = db.query(Policy).filter(
            Policy.dimension == dimension,
            Policy.app_id    == app_id,
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail=f"Policy '{dimension}' not found")

        # Include recent audit history for this dimension
        history = (
            db.query(PapAuditLog)
            .filter(PapAuditLog.entity_id == dimension)
            .order_by(PapAuditLog.timestamp.desc())
            .limit(10)
            .all()
        )
        result = p.to_dict()
        result["audit_history"] = [h.to_dict() for h in history]
        return result

    @router.get("/v1/policies/{dimension}/preview")
    def preview_policy_message(
        dimension: str,
        app_id:    Optional[str] = Query(None),
        role:      str           = Query("user_prompt"),
        verdict:   str           = Query("COERCIVE"),
        db:        Session = Depends(get_db),
    ):
        """
        Preview the rendered governance message for a policy dimension.

        Returns the Template A message that the MessageWriter would produce
        for the given verdict (COERCIVE or NORMATIVE) and role, using the
        policy's current policy_code, normative_action, and normative_guidance.

        Query params:
          role    — user_prompt | llm_response | tool_call | agent_to_agent
          verdict — COERCIVE | NORMATIVE
        """
        if verdict not in ("COERCIVE", "NORMATIVE"):
            raise HTTPException(status_code=422, detail="verdict must be COERCIVE or NORMATIVE")

        p = db.query(Policy).filter(
            Policy.dimension == dimension,
            Policy.app_id    == app_id,
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail=f"Policy '{dimension}' not found")

        if not p.policy_code:
            return {
                "dimension":    dimension,
                "verdict":      verdict,
                "role":         role,
                "rendered":     None,
                "warning":      "policy_code not set — MessageWriter cannot render a message for this policy",
            }

        # Import the MessageWriter from the policy engine (PAP shares the same process path)
        try:
            from core.message_context import (
                EvaluationContext, NormativeAction, PolicyViolation, RaisingEntity,
            )
            from core.message_writer import render_message

            norm_desc = p.remediation_normative or dimension
            coer_desc = p.remediation_coercive  or dimension
            pv = PolicyViolation(
                policy_code           = p.policy_code,
                normative_description = norm_desc,
                coercive_description  = coer_desc,
                normative_action      = NormativeAction(p.normative_action or "REVISION"),
                normative_guidance    = p.normative_guidance or norm_desc,
            )
            ctx = EvaluationContext(
                role           = role,
                ref_id         = "PREVIEW0",
                raising_entity = RaisingEntity.TRUST_PLATFORM,
            )
            rendered = render_message([pv], ctx, verdict)
            return {
                "dimension":    dimension,
                "policy_code":  p.policy_code,
                "verdict":      verdict,
                "role":         role,
                "rendered": {
                    "coercive":         rendered.coercive,
                    "normative":        rendered.normative,
                    "normative_action": rendered.normative_action,
                    "template_type":    rendered.template_type,
                },
            }
        except ImportError:
            # PAP may not share the policy-engine's Python path in all deployments
            return {
                "dimension":   dimension,
                "policy_code": p.policy_code,
                "verdict":     verdict,
                "role":        role,
                "rendered":    None,
                "warning":     "MessageWriter not available in this PAP deployment",
            }

    @router.put("/v1/policies/{dimension}")
    def update_policy(
        dimension: str,
        body:      PolicyUpdate,
        app_id:    Optional[str] = Query(None),
        db:        Session = Depends(get_db),
        actor:     str     = Depends(_get_actor),
    ):
        p = db.query(Policy).filter(
            Policy.dimension == dimension,
            Policy.app_id    == app_id,
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail=f"Policy '{dimension}' not found")

        old_vals = p.to_dict()

        if body.severity is not None:
            p.severity = body.severity
        if body.active is not None:
            p.active = body.active
        if body.hitl_trigger is not None:
            p.hitl_trigger = body.hitl_trigger
        if body.thresholds is not None:
            t = body.thresholds
            p.permissive_lo, p.permissive_hi = t.permissive
            p.normative_lo,  p.normative_hi  = t.normative
            p.coercive_lo,   p.coercive_hi   = t.coercive
        if body.remediation_normative is not None:
            p.remediation_normative = body.remediation_normative
        if body.remediation_coercive is not None:
            p.remediation_coercive = body.remediation_coercive
        if body.policy_code is not None:
            p.policy_code = body.policy_code or None
        if body.normative_action is not None:
            p.normative_action = body.normative_action
        if body.normative_guidance is not None:
            p.normative_guidance = body.normative_guidance or None
        if body.human_note is not None:
            p.human_note = body.human_note if body.human_note else None
        if body.ops_alert is not None:
            p.ops_alert = body.ops_alert
        if body.incident_routing is not None:
            p.incident_routing = body.incident_routing

        p.version    += 1
        p.updated_at  = datetime.utcnow()

        db.add(PapAuditLog(
            entity_type="policy", entity_id=p.dimension, app_id=p.app_id,
            action="UPDATE", actor=actor,
            old_values=json.dumps(old_vals),
            new_values=json.dumps(p.to_dict()),
        ))
        db.commit()
        db.refresh(p)
        logger.info(f"[PAP] UPDATE policy={dimension} version={p.version} actor={actor}")
        return p.to_dict()

    @router.delete("/v1/policies/{dimension}")
    def delete_policy(
        dimension: str,
        app_id:    Optional[str] = Query(None),
        db:        Session = Depends(get_db),
        actor:     str     = Depends(_get_actor),
    ):
        p = db.query(Policy).filter(
            Policy.dimension == dimension,
            Policy.app_id    == app_id,
        ).first()
        if not p:
            raise HTTPException(status_code=404, detail=f"Policy '{dimension}' not found")

        old_vals = p.to_dict()
        p.active     = False
        p.updated_at = datetime.utcnow()

        db.add(PapAuditLog(
            entity_type="policy", entity_id=p.dimension, app_id=p.app_id,
            action="DELETE", actor=actor,
            old_values=json.dumps(old_vals),
        ))
        db.commit()
        logger.info(f"[PAP] DELETE policy={dimension} actor={actor}")
        return {"deleted": True, "dimension": dimension}

    # ================================================================== #
    #  PUBLISH                                                            #
    # ================================================================== #

    @router.post("/v1/policies/publish")
    def publish_policies(
        body:  PublishRequest = PublishRequest(),
        db:    Session = Depends(get_db),
        actor: str     = Depends(_get_actor),
    ):
        """
        Serialize all active policies to policies.yaml and hot-reload Policy Engine.

        Also performs a dimension sync-check: fetches the active evaluation
        dimensions from the Guardrail Service and warns if any dimension has
        no corresponding PE policy (orphaned dimension → no verdict possible).
        """
        # ── Dimension sync-check (GS ↔ PE) ─────────────────────────────────
        sync_warnings = []
        try:
            gs_resp = httpx.get(f"{GUARDRAIL_URL}/v1/evaluations", timeout=4.0)
            if gs_resp.status_code == 200:
                gs_dims = {
                    e["dimension"]
                    for e in gs_resp.json().get("evaluations", [])
                    if e.get("status") == "active"
                }
                pe_dims = {
                    p.dimension
                    for p in db.query(Policy).filter(Policy.active == True, Policy.app_id == None).all()
                }
                orphaned = gs_dims - pe_dims
                if orphaned:
                    for d in sorted(orphaned):
                        msg = f"GS dimension '{d}' has no PE policy — evaluations will score but produce no verdict"
                        sync_warnings.append(msg)
                        logger.warning(f"[PAP] SYNC WARNING: {msg}")
        except Exception as e:
            sync_warnings.append(f"Sync-check skipped (GS unreachable): {e}")

        result = _publish(db, actor=actor, notes=body.notes or "")
        if sync_warnings:
            result["sync_warnings"] = sync_warnings
        status_code = 200 if result.get("success") else 500
        return JSONResponse(status_code=status_code, content=result)

    @router.put("/v1/governance/{dimension}/enabled")
    def set_dimension_enabled(
        dimension: str,
        body:      dict,                      # {"enabled": bool}
        db:        Session = Depends(get_db),
        actor:     str     = Depends(_get_actor),
    ):
        """
        THE single, idiot-proof enable/disable switch for a governance dimension.

        Sets BOTH config_policies.active AND config_evaluation_dimensions.status in
        one transaction so the Evaluation Catalog (active/deferred) and the Policies
        tab (enabled/disabled) can NEVER disagree, then:
          • reloads the Guardrail Service taxonomy (it stops/starts scoring the dim), and
          • hot-reloads the Policy Engine (disabled_dims + deferred_dims refresh).
        Net effect — applied live, no restart:
          enabled  → dimension active  + policy active  → ENFORCED (can block)
          disabled → dimension deferred + policy inactive → NO governance (PERMISSIVE)
        """
        enabled    = bool(body.get("enabled", True))
        new_status = "active" if enabled else "deferred"

        pol = db.query(Policy).filter(
            Policy.dimension == dimension, Policy.app_id == None
        ).first()
        dim = db.query(EvaluationDimension).filter(
            EvaluationDimension.dimension_id == dimension
        ).first()
        if pol is None and dim is None:
            raise HTTPException(
                status_code=404,
                detail=f"No policy or dimension found for '{dimension}'.",
            )

        changed: dict = {}
        if pol is not None and pol.active != enabled:
            changed["policy_active"] = {"from": pol.active, "to": enabled}
            pol.active     = enabled
            pol.version   += 1
            pol.updated_at = datetime.utcnow()
        if dim is not None and dim.status != new_status:
            changed["dimension_status"] = {"from": dim.status, "to": new_status}
            dim.status      = new_status
            dim.updated_at  = datetime.utcnow()

        db.add(PapAuditLog(
            entity_type="governance_dimension", entity_id=dimension, app_id=None,
            action="UPDATE", actor=actor,
            old_values=json.dumps({k: v["from"] for k, v in changed.items()}),
            new_values=json.dumps({k: v["to"]   for k, v in changed.items()}),
        ))
        db.commit()

        # 1) GS taxonomy: start/stop scoring this dimension (hot)
        gs_reloaded = False
        try:
            gr = httpx.post(f"{GUARDRAIL_URL}/internal/taxonomy/reload", timeout=6.0)
            gs_reloaded = (gr.status_code == 200)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[PAP] GS taxonomy reload failed during enable-toggle: {e}")

        # 2) PE: hot-reload policies (disabled_dims) + deferred_dims refresh
        pub = _publish(db, actor=actor, notes=f"enable-toggle {dimension}={enabled}")

        return {
            "dimension":        dimension,
            "enabled":          enabled,
            "dimension_status": new_status,
            "policy_active":    enabled,
            "changed":          changed,
            "gs_reloaded":      gs_reloaded,
            "pe_published":     bool(pub.get("success")),
            "reload_warning":   pub.get("reload_warning"),
        }

    # ================================================================== #
    #  EVALUATORS                                                         #
    #  GET  /v1/evaluators                  → list + policy coverage      #
    #  GET  /v1/evaluators/{dimension}      → single evaluator + policy   #
    #  POST /v1/evaluators/{dimension}/policy → upsert policy + publish   #
    # ================================================================== #

    @router.get("/v1/evaluators")
    def list_evaluators(db: Session = Depends(get_db)):
        """
        Return all evaluators from the Guardrail Service joined with their
        current policy status from the PAP database.

        Each entry includes:
          - dimension      : evaluator id
          - provider       : scoring method (e.g. mock_keyword, embedding_classifier)
          - category       : evaluator category group
          - status         : active | deferred | disabled (from GS)
          - has_policy     : whether a global policy exists in the PAP DB
          - policy         : the policy record if it exists, else null

        This endpoint drives the "Define policy for evaluator" demo flow.
        """
        # ── Fetch evaluators from Guardrail Service ───────────────────────
        gs_evaluators = []
        try:
            r = httpx.get(f"{GUARDRAIL_URL}/v1/evaluations", timeout=5.0)
            if r.status_code == 200:
                gs_evaluators = r.json().get("evaluations", [])
        except Exception as e:
            logger.warning(f"[PAP] /v1/evaluators: GS unreachable — {e}")

        # ── Fetch all global policies from PAP DB ─────────────────────────
        policies = {
            p.dimension: p
            for p in db.query(Policy).filter(
                Policy.active == True,
                Policy.app_id == None,
            ).all()
        }

        # ── Join ──────────────────────────────────────────────────────────
        result = []
        for ev in gs_evaluators:
            dim = ev.get("dimension", "")
            pol = policies.get(dim)
            result.append({
                "dimension":  dim,
                "provider":   ev.get("provider") or ev.get("evaluator_type", ""),
                "category":   ev.get("category", ""),
                "status":     ev.get("status", "unknown"),
                "has_policy": pol is not None,
                "policy":     pol.to_dict() if pol else None,
            })

        # Sort: evaluators without a policy first (need attention), then alpha
        result.sort(key=lambda e: (e["has_policy"], e["dimension"]))

        return {
            "total":      len(result),
            "evaluators": result,
            "without_policy": sum(1 for e in result if not e["has_policy"]),
        }

    @router.get("/v1/evaluators/{dimension}")
    def get_evaluator(dimension: str, db: Session = Depends(get_db)):
        """
        Return a single evaluator from the Guardrail Service joined with its
        current policy.  Returns 404 if the evaluator is not registered in the GS.
        """
        # ── GS lookup ─────────────────────────────────────────────────────
        gs_ev = None
        try:
            r = httpx.get(f"{GUARDRAIL_URL}/v1/evaluations", timeout=5.0)
            if r.status_code == 200:
                for ev in r.json().get("evaluations", []):
                    if ev.get("dimension") == dimension:
                        gs_ev = ev
                        break
        except Exception as e:
            logger.warning(f"[PAP] /v1/evaluators/{dimension}: GS unreachable — {e}")

        if gs_ev is None:
            raise HTTPException(
                status_code=404,
                detail=f"Evaluator '{dimension}' not found in Guardrail Service.",
            )

        pol = db.query(Policy).filter(
            Policy.dimension == dimension,
            Policy.app_id    == None,
            Policy.active    == True,
        ).first()

        return {
            "dimension":  dimension,
            "provider":   gs_ev.get("provider") or gs_ev.get("evaluator_type", ""),
            "category":   gs_ev.get("category", ""),
            "status":     gs_ev.get("status", "unknown"),
            "has_policy": pol is not None,
            "policy":     pol.to_dict() if pol else None,
        }

    @router.post("/v1/evaluators/{dimension}/policy", status_code=201)
    def define_evaluator_policy(
        dimension: str,
        body:      EvaluatorPolicyUpsert,
        db:        Session = Depends(get_db),
        actor:     str     = Depends(_get_actor),
    ):
        """
        Define (create or update) a policy for a specific evaluator, then
        optionally publish to policies.yaml and hot-reload the Policy Engine.

        This is the primary entry point for the "Define policy for evaluator"
        demo workflow — a single call creates the policy and activates it.

        - If a policy for this dimension already exists it is updated in-place
          (version incremented, audit trail written).
        - If publish=True (default), policies.yaml is rewritten and the PE
          reloads its policy registry so the new policy takes effect immediately.

        Returns:
          {
            "policy":         { ... },          ← full policy record
            "action":         "created"|"updated",
            "published":      true|false,
            "publish_result": { ... } | null    ← present when published=true
          }
        """
        t = body.thresholds
        existing = db.query(Policy).filter(
            Policy.dimension == dimension,
            Policy.app_id    == body.app_id,
        ).first()

        if existing:
            # ── Update path ───────────────────────────────────────────────
            old_vals = existing.to_dict()
            existing.severity              = body.severity
            existing.permissive_lo, existing.permissive_hi = t.permissive
            existing.normative_lo,  existing.normative_hi  = t.normative
            existing.coercive_lo,   existing.coercive_hi   = t.coercive
            existing.remediation_normative = body.remediation_normative
            existing.remediation_coercive  = body.remediation_coercive
            existing.policy_code           = body.policy_code or None
            existing.normative_action      = body.normative_action
            existing.normative_guidance    = body.normative_guidance or None
            existing.human_note            = body.human_note or None
            existing.ops_alert             = body.ops_alert
            existing.incident_routing      = body.incident_routing
            existing.active                = True
            existing.version              += 1
            existing.updated_at            = datetime.utcnow()

            db.add(PapAuditLog(
                entity_type="policy", entity_id=dimension, app_id=body.app_id,
                action="UPDATE", actor=actor,
                old_values=json.dumps(old_vals),
                new_values=json.dumps(existing.to_dict()),
            ))
            db.commit()
            db.refresh(existing)
            policy_record = existing
            action = "updated"
            logger.info(f"[PAP] UPSERT(update) evaluator policy={dimension} actor={actor}")
        else:
            # ── Create path ───────────────────────────────────────────────
            policy_record = Policy(
                dimension=dimension,
                app_id=body.app_id,
                severity=body.severity,
                permissive_lo=t.permissive[0], permissive_hi=t.permissive[1],
                normative_lo=t.normative[0],   normative_hi=t.normative[1],
                coercive_lo=t.coercive[0],     coercive_hi=t.coercive[1],
                remediation_normative=body.remediation_normative,
                remediation_coercive=body.remediation_coercive,
                policy_code=body.policy_code or None,
                normative_action=body.normative_action,
                normative_guidance=body.normative_guidance or None,
                human_note=body.human_note or None,
                ops_alert=body.ops_alert,
                incident_routing=body.incident_routing,
            )
            db.add(policy_record)
            db.flush()
            db.add(PapAuditLog(
                entity_type="policy", entity_id=dimension, app_id=body.app_id,
                action="CREATE", actor=actor,
                new_values=json.dumps(policy_record.to_dict()),
            ))
            db.commit()
            db.refresh(policy_record)
            action = "created"
            logger.info(f"[PAP] UPSERT(create) evaluator policy={dimension} actor={actor}")

        # ── Optional immediate publish ─────────────────────────────────────
        publish_result = None
        if body.publish:
            publish_result = _publish(db, actor=actor, notes=f"Auto-publish after {action} policy for {dimension}")
            logger.info(f"[PAP] evaluator policy {dimension} published — {publish_result.get('published_count', '?')} policies written")

        return JSONResponse(
            status_code=201,
            content={
                "policy":         policy_record.to_dict(),
                "action":         action,
                "published":      body.publish,
                "publish_result": publish_result,
            },
        )

    # ================================================================== #
    #  APP CONFIGS                                                        #
    # ================================================================== #

    @router.get("/v1/app-configs")
    def list_app_configs(db: Session = Depends(get_db)):
        configs = db.query(App).order_by(App.app_id).all()
        return {"total": len(configs), "app_configs": [c.to_dict() for c in configs]}

    @router.post("/v1/app-configs", status_code=201)
    def upsert_app_config(
        body:  AppUpsert,
        db:    Session = Depends(get_db),
        actor: str     = Depends(_get_actor),
    ):
        cfg = db.query(App).filter(App.app_id == body.app_id).first()
        if cfg:
            cfg.display_name       = body.display_name
            cfg.base_tier          = body.base_tier
            cfg.dynamic_evaluation = body.dynamic_evaluation
            cfg.updated_at         = datetime.utcnow()
            db.commit()
            db.refresh(cfg)
            return cfg.to_dict()

        cfg = App(
            app_id=body.app_id,
            display_name=body.display_name,
            base_tier=body.base_tier,
            dynamic_evaluation=body.dynamic_evaluation,
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
        return JSONResponse(status_code=201, content=cfg.to_dict())

    @router.delete("/v1/app-configs/{app_id}")
    def delete_app_config(app_id: str, db: Session = Depends(get_db)):
        cfg = db.query(App).filter(App.app_id == app_id).first()
        if not cfg:
            raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")
        db.delete(cfg)
        db.commit()
        return {"deleted": True, "app_id": app_id}

    # ================================================================== #
    #  AUDIT LOG                                                          #
    # ================================================================== #

    @router.get("/v1/audit")
    def get_audit_log(
        dimension: Optional[str] = Query(None),
        action:    Optional[str] = Query(None),
        limit:     int = Query(100, ge=1, le=500),
        offset:    int = Query(0, ge=0),
        db: Session = Depends(get_db),
    ):
        q = db.query(PapAuditLog)
        if dimension:
            q = q.filter(PapAuditLog.entity_id == dimension)
        if action:
            q = q.filter(PapAuditLog.action == action.upper())
        total = q.count()
        entries = (
            q.order_by(PapAuditLog.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "total":   total,
            "limit":   limit,
            "offset":  offset,
            "entries": [e.to_dict() for e in entries],
        }

    # ================================================================== #
    #  AUDIT — EVALUATIONS (Phase 3)                                     #
    # ================================================================== #

    # JSONL path kept only for the SSE live-tail endpoint (stream_evaluation_audit).
    # All history queries now read from SQLite via audit_db.load_audit_records().
    _EVAL_AUDIT_PATH = (
        Path(__file__).resolve().parent.parent.parent
        / "policy-engine" / "logs" / "audit.jsonl"
    )

    @router.get("/v1/audit/evaluations")
    def get_evaluation_audit(
        limit:   int            = Query(50, ge=1, le=2000),
        verdict: Optional[str]  = Query(None, description="Filter by verdict: PERMISSIVE | NORMATIVE | COERCIVE"),
        app_id:  Optional[str]  = Query(None, description="Filter by application ID"),
        window_minutes: Optional[int] = Query(
            None, ge=1, le=10080,
            description="Restrict to records within the last N minutes (uses the timestamp index — keeps queries fast as the audit table grows).",
        ),
        db:      Session        = Depends(get_db),
    ):
        """
        Read recent evaluation audit records from the PAP SQLite database.

        Records are returned newest-first. Persistent across Policy Engine
        restarts and log rotation — source of truth is audit_evaluation_audit.
        """
        # Force a fresh read snapshot.  SQLAlchemy with SQLite WAL mode uses a
        # deferred transaction; if the connection was reused from the pool with an
        # old snapshot the PE's most-recent writes would be invisible.  Rolling back
        # the idle transaction before querying ensures we always read committed data.
        try:
            db.rollback()
        except Exception:
            pass
        from api.audit_db import load_audit_records
        _since = None
        if window_minutes is not None:
            from datetime import datetime, timezone, timedelta
            _since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        records = load_audit_records(
            db, limit=limit, verdict=verdict, app_id=app_id, since=_since
        )
        if not records:
            return {
                "total":   0,
                "limit":   limit,
                "records": [],
                "message": (
                    "No evaluation audit records found yet. "
                    "Run at least one evaluation through the Policy Engine first."
                ),
            }
        return {"total": len(records), "limit": limit, "records": records}

    @router.get("/api/session/{session_id}/interactions")
    def get_session_interactions_endpoint(
        session_id: str,
        db: Session = Depends(get_db),
    ):
        """
        Return interaction events from audit_interaction_events for a session.

        Used by the dashboard interaction graph renderer (_igRenderColumnarSvg).
        Events are sorted by seq_in_session (ascending) so the renderer sees
        them in conversation order.

        Falls back to an empty events list if the table has not yet been
        created (old deployments where SqliteAuditWriter hasn't written events).
        """
        # Force SQLAlchemy to discard any cached snapshot from this connection.
        # Without this, WAL-mode SQLite may return a stale read from a transaction
        # snapshot taken before the PE wrote new interaction events (same pattern
        # as the fix applied to /v1/audit/evaluations).
        try:
            db.rollback()
        except Exception:
            pass
        from api.audit_db import get_session_interactions
        events = get_session_interactions(db, session_id)
        return {"session_id": session_id, "events": events}

    @router.get("/v1/audit/stream")
    async def stream_evaluation_audit(
        request: Request,
        verdict: Optional[str] = Query(None, description="Filter by verdict: PERMISSIVE | NORMATIVE | COERCIVE"),
    ):
        """
        Server-Sent Events stream of evaluation audit records (tail -f equivalent).

        Connects to the Policy Engine's audit.jsonl file and emits new records
        as they are appended.  Polls every 400 ms; the client stays connected
        until it disconnects.  Each SSE event carries a single JSON record.

        Usage (JavaScript):
            const es = new EventSource('/v1/audit/stream');
            es.onmessage = e => console.log(JSON.parse(e.data));
        """
        _verdict_filter = verdict.upper() if verdict else None

        async def _generator():
            # Yield the SSE "retry" hint so browsers reconnect within 2 s if
            # the connection drops (e.g. PAP restart during dev).
            yield "retry: 2000\n\n"

            # Start tailing from the current end-of-file so we only emit NEW
            # records written after the client connects (not historical data).
            offset = 0
            if _EVAL_AUDIT_PATH.exists():
                try:
                    offset = _EVAL_AUDIT_PATH.stat().st_size
                except OSError:
                    offset = 0

            while True:
                await asyncio.sleep(0.4)          # 400 ms poll cadence

                if await request.is_disconnected():
                    logger.debug("SSE client disconnected — closing audit stream")
                    break

                if not _EVAL_AUDIT_PATH.exists():
                    continue

                try:
                    size = _EVAL_AUDIT_PATH.stat().st_size
                except OSError:
                    continue

                if size <= offset:
                    # Nothing new — send a keep-alive comment so the connection
                    # is not closed by proxies that enforce idle timeouts.
                    yield ": keep-alive\n\n"
                    continue

                try:
                    with _EVAL_AUDIT_PATH.open("r", encoding="utf-8") as fh:
                        fh.seek(offset)
                        new_content = fh.read()
                    offset = size
                except OSError:
                    continue

                for raw_line in new_content.splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        rec = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if _verdict_filter and rec.get("verdict") != _verdict_filter:
                        continue
                    yield f"data: {json.dumps(rec)}\n\n"

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control":    "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx/proxy buffering
                "Connection":       "keep-alive",
            },
        )

    # ================================================================== #
    #  TIER 1 METRICS (SRS observability requirements)                   #
    # ================================================================== #

    @router.get("/v1/metrics/tier1")
    def tier1_metrics(
        limit:          int            = Query(500, ge=10, le=5000),
        window_minutes: Optional[int]  = Query(None, ge=1, le=10080,
                                               description="Only include records from the last N minutes. Omit for all-time."),
        app_id:         Optional[str]  = Query(None, description="Filter metrics to a specific application. Omit for platform-wide."),
        db:             Session        = Depends(get_db),
    ):
        """
        Compute SRS Tier 1 metrics from the PAP SQLite database.

        Source: audit_evaluation_audit + audit_evaluation_dimensions.
        Persistent across PE restarts and log rotation.

        Query parameters:
          limit          — max audit records to scan (most recent first)
          window_minutes — restrict to records within the last N minutes

        Metric status values:
          available         — computed from live audit data
          no_data           — audit records exist but no matching traffic yet
          deferred_dim      — dimension registered but not active in GS
          requires_otel     — needs Arize/OTel instrumentation (Phase 5)
          requires_arize    — needs Arize eval integration
        """
        import math
        from datetime import datetime, timezone, timedelta
        from api.audit_db import load_audit_records

        window_cutoff = None
        if window_minutes is not None:
            window_cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

        records = load_audit_records(
            db,
            limit=limit,
            since=window_cutoff,
            app_id=app_id,
        )

        if not records:
            return {"computed_from": 0, "metrics": {}, "message": "No audit data yet."}

        # ── Partition by role ──────────────────────────────────────────────
        prompt_records   = [r for r in records if r.get("role") == "user_prompt"]
        response_records = [r for r in records if r.get("role") == "llm_response"]
        a2a_records      = [r for r in records if r.get("role") == "agent_to_agent"]
        tool_records     = [r for r in records if r.get("role") == "tool_call"]

        # ── Verdict counts (across all roles) ─────────────────────────────
        coercive_count  = sum(1 for r in records if r.get("verdict") == "COERCIVE")
        normative_count = sum(1 for r in records if r.get("verdict") == "NORMATIVE")

        # ── Helpers ────────────────────────────────────────────────────────
        SAFETY_DIMS  = {"hate", "harassment", "violence", "sexual_content", "self_harm", "misconduct"}
        TOXICITY_DIMS = {"hate", "harassment", "violence", "sexual_content"}

        def _dim_score(record: dict, dim: str):
            for d in record.get("dimension_results", []):
                if d.get("dimension") == dim:
                    return d.get("score")
            return None

        def _worst_dim(record: dict, dims: set):
            scores = [s for s in (_dim_score(record, d) for d in dims) if s is not None]
            return max(scores) if scores else None

        def _normalize_safe(score_1_9) -> float:
            """1-9 → 0-1 where 1.0=perfectly safe, 0.0=maximally unsafe."""
            return round(1.0 - (score_1_9 - 1) / 8, 3)

        def _normalize_toxic(score_1_9) -> float:
            """1-9 → 0-1 where 0.0=clean, 1.0=maximally toxic."""
            return round((score_1_9 - 1) / 8, 3)

        # _metrics_avg / _metrics_p95 are module-level helpers — never shadow them locally.

        # ── 1. Latency ─────────────────────────────────────────────────────
        latencies = [r["latency_ms"] for r in records if r.get("latency_ms")]

        # ── 2. Prompt safety score ─────────────────────────────────────────
        # Include ALL user_prompt records that have safety dim scores — not
        # just non-PERMISSIVE ones.  Score aggregates should reflect what the
        # guardrail *detected*, not what the policy *decided*.  A PERMISSIVE
        # verdict on a clean prompt (score=1) is valid data and keeps the
        # sample growing with every test.  Enforcement-gating belongs only on
        # flag/count metrics (PII, SDR, injection) where we count incidents.
        prompt_safety = []
        for r in prompt_records:
            w = _worst_dim(r, SAFETY_DIMS)
            if w is not None:
                prompt_safety.append(_normalize_safe(w))

        # ── 3. Response toxicity score ─────────────────────────────────────
        # Include ALL llm_response records that have toxicity dim scores.
        # Same rationale as prompt_safety: score averages should reflect
        # guardrail detections, not policy verdicts.  A clean response
        # (score=0.0) should contribute to the average so the sample grows
        # with every turn.
        response_toxicity = []
        for r in response_records:
            w = _worst_dim(r, TOXICITY_DIMS)
            if w is not None:
                response_toxicity.append(_normalize_toxic(w))

        # ── 4. Sensitive data disclosure flag ──────────────────────────────
        # Enforcement-gated: count only records where sensitive_data_disclosure
        # scored >= 4 AND the overall verdict was non-PERMISSIVE.
        # Rationale: the Guardrail Service always scores every active
        # dimension and writes raw scores to the audit record regardless of
        # whether the operator has the policy enabled.  Counting raw scores
        # when a policy is disabled produces false positives — the Platform
        # appears to be detecting PII incidents that it has been told to
        # ignore.  Gating on verdict ensures the metric reflects actual
        # enforced detections, matching how Azure / AWS surface PII counts.
        pii_flagged = sum(
            1 for r in records
            if r.get("verdict") != "PERMISSIVE"
            and any(d.get("dimension") == "sensitive_data_disclosure" and (d.get("score") or 0) >= 4
                    for d in r.get("dimension_results", []))
        )

        # ── 4b. Sensitive data request flag ────────────────────────────────
        # Counts user_prompt evaluations where sensitive_data_request scored
        # >= 4 (NORMATIVE or COERCIVE) AND verdict was non-PERMISSIVE.
        # Distinct from sensitive_data_disclosure:
        #   sensitive_data_request    = user ASKED for sensitive data (intent)
        #   sensitive_data_disclosure = regulated identifiers appeared in LLM output
        # Scoped to user_prompt records only — this dimension is not active
        # on other interception points.
        sdr_flagged = sum(
            1 for r in prompt_records
            if r.get("verdict") != "PERMISSIVE"
            and any(d.get("dimension") == "sensitive_data_request" and (d.get("score") or 0) >= 4
                    for d in r.get("dimension_results", []))
        )

        # ── 5. Prompt injection detection ──────────────────────────────────
        inj_flagged = sum(
            1 for r in records
            if any(d.get("dimension") == "prompt_injection" and (d.get("score") or 0) >= 7
                   for d in r.get("dimension_results", []))
        )
        # Derive active/deferred status from audit data already in memory —
        # avoids a blocking 3-second GS network call on every 10-second T1
        # refresh cycle.  If ANY record carries a non-null prompt_injection
        # score the dimension is clearly active.  If no scores exist the GS
        # is not dispatching to that dimension (deferred or removed from the
        # mandatory pack) — mark as deferred_dim so the UI shows "not scored"
        # (grey bar, no traffic tooltip) rather than a misleading 100% pass rate.
        has_inj_scores = any(
            d.get("dimension") == "prompt_injection" and d.get("score") is not None
            for r in records
            for d in r.get("dimension_results", [])
        )
        inj_status = "available" if has_inj_scores else "deferred_dim"

        # ── 6. A2A risk score ─────────────────────────────────────────────
        # Uses the WORST dimension across ALL dims on agent_to_agent evals,
        # not just safety dims.  This captures sanctions, sensitive_data_disclosure,
        # and other compliance/security violations that appear on the A2A channel.
        # A score of 1.0 = all dims clean; 0.0 = at least one dim at max (9).
        ALL_DIMS_SENTINEL = None  # None → _worst_dim scans all dimension_results
        a2a_safety = []
        for r in a2a_records:
            all_scores = [d.get("score") for d in r.get("dimension_results", [])
                          if d.get("score") is not None]
            if all_scores:
                a2a_safety.append(_normalize_safe(max(all_scores)))

        # ── 7. Token usage (tokens/request) ───────────────────────────────
        # Compute from agent_tokens in eval metadata — no OTel required.
        # One "request" = one session_id. We deduplicate per (session, agent)
        # by keeping only the largest snapshot (latest in execution order),
        # then sum all agents per session to get total tokens for that request.
        _tok_snaps: Dict[tuple, dict] = {}
        for r in records:
            meta = r.get("metadata") or {}
            at   = meta.get("agent_tokens") or {}
            if not isinstance(at, dict) or at.get("total_tokens", 0) == 0:
                continue
            sid   = r.get("session_id") or "_no_session"
            agent = meta.get("agent_name") or "_unknown"
            key   = (sid, agent)
            if at.get("total_tokens", 0) > _tok_snaps.get(key, {}).get("total_tokens", 0):
                _tok_snaps[key] = at

        # Sum per session → one value per request
        _session_tokens: Dict[str, int] = {}
        for (sid, _agent), snap in _tok_snaps.items():
            _session_tokens[sid] = _session_tokens.get(sid, 0) + snap.get("total_tokens", 0)

        token_per_req_list = list(_session_tokens.values())

        # ── 8. Agent step success flag ─────────────────────────────────────
        tool_total   = len(tool_records)
        tool_blocked = sum(1 for r in tool_records if r.get("verdict") == "COERCIVE")
        tool_success_rate = round((tool_total - tool_blocked) / tool_total * 100, 1) if tool_total else None

        # ── 9. A2A invocation count (avg handoffs per session) ────────────
        a2a_sessions = {r.get("session_id") for r in a2a_records if r.get("session_id")}
        a2a_inv_avg  = round(len(a2a_records) / len(a2a_sessions), 2) if a2a_sessions else None

        # ── 10. A2A violation rate ─────────────────────────────────────────
        a2a_violations = sum(1 for r in a2a_records if r.get("verdict") in ("COERCIVE", "NORMATIVE"))
        a2a_viol_rate  = round(a2a_violations / len(a2a_records) * 100, 1) if a2a_records else None

        # ── 11. Unsanctioned A2A flag ──────────────────────────────────────
        # An A2A eval for an agent not in the declared registry is a governance
        # gap: an out-of-band inter-agent handoff that bypassed instrument().
        try:
            _agq = db.query(AppAgent)
            if app_id:
                _agq = _agq.filter(AppAgent.app_id == app_id)
            _declared_agents = {r.agent_name for r in _agq.all() if r.is_declared_agent}
        except Exception:
            _declared_agents = set()

        a2a_unsanctioned = 0
        for r in a2a_records:
            meta  = r.get("metadata") or {}
            aname = meta.get("agent_name") if isinstance(meta, dict) else None
            if aname and aname not in _declared_agents:
                a2a_unsanctioned += 1

        # ── dim_scores — per-evaluation-dimension breakdown ──────────────
        # Aggregated from dimension_results already in memory (no extra query).
        # Joined with config_evaluation_dimensions for category + display_name.
        # Sorted: most-flagged first, then by category, then alphabetically.
        _dim_catalog: dict = {}
        try:
            for _d in db.query(EvaluationDimension).all():
                _dim_catalog[_d.dimension_id] = {
                    "category":     _d.category,
                    "display_name": _d.display_name,
                    "status":       _d.status,
                }
        except Exception:
            pass  # catalog join is best-effort; dim_scores still render without it

        _dim_agg: Dict[str, dict] = {}
        for _r in records:
            for _dr in _r.get("dimension_results", []):
                _dim_id = _dr.get("dimension") or _dr.get("dimension_id")
                if not _dim_id:
                    continue
                _s = _dr.get("score")
                if _s is None:
                    continue
                if _dim_id not in _dim_agg:
                    _dim_agg[_dim_id] = {"scores": [], "flagged": 0}
                _dim_agg[_dim_id]["scores"].append(_s)
                if _s >= 7:
                    _dim_agg[_dim_id]["flagged"] += 1

        dim_scores_list: list = []
        for _dim_id, _agg in _dim_agg.items():
            _sc      = _agg["scores"]
            _meta    = _dim_catalog.get(_dim_id, {})
            _dim_avg = round(sum(_sc) / len(_sc), 2)   # local; must NOT shadow _metrics_avg()
            dim_scores_list.append({
                "dimension_id":       _dim_id,
                "category":           _meta.get("category", "other"),
                "display_name":       _meta.get("display_name") or _dim_id.replace("_", " ").title(),
                "avg_score":          _dim_avg,
                "avg_normalized":     round((_dim_avg - 1) / 8, 3),
                "flagged_count":      _agg["flagged"],
                "violation_rate_pct": round(_agg["flagged"] / len(_sc) * 100, 1) if _sc else 0,
                "sample":             len(_sc),
                "status":             _meta.get("status", "active"),
            })
        dim_scores_list.sort(key=lambda _x: (-_x["flagged_count"], _x["category"], _x["dimension_id"]))

        # ── Assemble response ──────────────────────────────────────────────
        total = len(records)
        return {
            "computed_from": total,
            "window_records": limit,
            "dim_scores": dim_scores_list,
            "role_breakdown": {
                "user_prompt":   len(prompt_records),
                "llm_response":  len(response_records),
                "agent_to_agent": len(a2a_records),
                "tool_call":     len(tool_records),
            },
            "metrics": {
                "latency_ms": {
                    "label":   "Governance latency (ms)",
                    "avg":     _metrics_avg(latencies),
                    "p95":     _metrics_p95(latencies),
                    "sample":  len(latencies),
                    "type":    "integer",
                    "source":  "policy_engine",
                    "status":  "available" if latencies else "no_data",
                    "note":    "PE evaluation round-trip added to each governed request.",
                    "formula": "avg(latency_ms) · p95(latency_ms)",
                },
                "token_usage": {
                    "label":   "Token usage (tokens/request)",
                    "avg":     int(round(_metrics_avg(token_per_req_list))) if token_per_req_list else None,
                    "sample":  len(token_per_req_list),
                    "type":    "integer",
                    "source":  "eval_metadata",
                    "status":  "available" if token_per_req_list else "no_data",
                    "note":    "LLM tokens per session (graph-mode runs only).",
                    "formula": "Σ max(agent_token_snapshot per agent) per session / unique_sessions",
                },
                "violation_rate": {
                    "label":        "Violation rate",
                    "flagged":      coercive_count,
                    "total":        total,
                    "flag_rate_pct": round(coercive_count / total * 100, 1) if total else 0,
                    "type":         "boolean",
                    "source":       "policy_engine",
                    "status":       "available" if records else "no_data",
                    "note":         "COERCIVE blocks as % of all evaluated interactions.",
                    "formula":      "COERCIVE_count / total_evals × 100%",
                },
                "prompt_safety_score": {
                    "label":   "Prompt safety score (0–1)",
                    "avg":     _metrics_avg(prompt_safety),
                    "sample":  len(prompt_safety),
                    "type":    "float",
                    "scale":   "1.0=safe, 0.0=unsafe",
                    "source":  "policy_engine",
                    "status":  "available" if prompt_safety else "no_data",
                    "note":    "Worst safety signal on user_prompt evals (1.0=safe).",
                    "formula": "avg(1 − (max(safety_dims) − 1) / 8) over user_prompt evals",
                },
                "response_toxicity_score": {
                    "label":   "Response toxicity score (0–1)",
                    "avg":     _metrics_avg(response_toxicity),
                    "sample":  len(response_toxicity),
                    "type":    "float",
                    "scale":   "0.0=clean, 1.0=toxic",
                    "source":  "policy_engine",
                    "status":  "available" if response_toxicity else "no_data",
                    "note":    "Worst toxicity signal on llm_response evals (0.0=clean).",
                    "formula": "avg((max(toxicity_dims) − 1) / 8) over llm_response evals",
                },
                "pii_exposure_flag": {
                    "label":        "Sensitive Data Disclosure",
                    "flagged":      pii_flagged,
                    "total":        total,
                    "flag_rate_pct": round(pii_flagged / total * 100, 1) if total else 0,
                    "type":         "boolean",
                    "source":       "policy_engine",
                    "status":       "available",
                    "note":         "Enforced sensitive_data_disclosure detections (regulated identifiers in LLM output). Distinct from sensitive_data_request (user intent).",
                    "formula":      "Σ(sensitive_data_disclosure_score ≥ 4 AND verdict ≠ PERMISSIVE) / total_evals",
                },
                "sensitive_data_request_flag": {
                    "label":        "Sensitive data request flag",
                    "flagged":      sdr_flagged,
                    "total":        len(prompt_records),
                    "flag_rate_pct": round(sdr_flagged / len(prompt_records) * 100, 1) if prompt_records else 0,
                    "type":         "boolean",
                    "source":       "policy_engine",
                    "status":       "available" if prompt_records else "no_data",
                    "note":         "User intent to request PII or credentials (prompt evals only).",
                    "formula":      "Σ(sensitive_data_request ≥ 4 AND verdict ≠ PERMISSIVE) / user_prompt_total",
                },
                "prompt_injection_detection": {
                    "label":        "Prompt injection detection",
                    "flagged":      inj_flagged,
                    "total":        total,
                    "flag_rate_pct": round(inj_flagged / total * 100, 1) if total else 0,
                    "type":         "boolean",
                    "source":       "policy_engine",
                    "status":       inj_status,
                    "note":         "Injection attempts via prompt_injection dim (score ≥ 7).",
                    "formula":      "Σ(prompt_injection_score ≥ 7) / total_evals",
                },
                "a2a_safety_score": {
                    "label":   "A2A Risk Score (0–1)",
                    "avg":     _metrics_avg(a2a_safety),
                    "sample":  len(a2a_safety),
                    "type":    "float",
                    "scale":   "1.0=clean, 0.0=violation",
                    "source":  "policy_engine",
                    "status":  "available" if a2a_safety else ("no_data" if a2a_records else "no_a2a_traffic"),
                    "note":    f"Worst policy signal across agent-to-agent channel. {len(a2a_records)} A2A eval(s).",
                    "formula": "avg(1 − (max(all_dims) − 1) / 8) over agent_to_agent evals",
                },
                "agent_step_success_flag": {
                    "label":           "Agent step success flag",
                    "total_steps":     tool_total,
                    "blocked_steps":   tool_blocked,
                    "success_rate_pct": tool_success_rate,
                    "type":            "boolean",
                    "source":          "policy_engine",
                    "status":          "available" if tool_records else "no_tool_traffic",
                    "note":            "Tool call (agent step) governance pass rate.",
                    "formula":         "(total_tool_calls − COERCIVE_tool_calls) / total_tool_calls × 100%",
                },
                # ── New A2A / agent metrics ─────────────────────────────────
                "a2a_invocation_count": {
                    "label":   "A2A invocations (avg/session)",
                    "avg":     a2a_inv_avg,
                    "total":   len(a2a_records),
                    "sessions": len(a2a_sessions),
                    "type":    "float",
                    "unit":    "avg/session",
                    "source":  "policy_engine",
                    "status":  "available" if a2a_records else "no_a2a_traffic",
                    "note":    "Avg agent handoffs per session; baseline for anomaly detection.",
                    "formula": "len(a2a_evals) / unique_sessions_with_a2a",
                },
                "a2a_violation_rate": {
                    "label":        "A2A violation rate",
                    "flagged":      a2a_violations,
                    "total":        len(a2a_records),
                    "flag_rate_pct": a2a_viol_rate,
                    "type":         "boolean",
                    "source":       "policy_engine",
                    "status":       "available" if a2a_records else "no_a2a_traffic",
                    "note":         "% of A2A evals returning COERCIVE or NORMATIVE verdict.",
                    "formula":      "Σ(verdict ∈ {COERCIVE, NORMATIVE}) / total_a2a_evals × 100%",
                },
                "agent_call_depth": {
                    "label":   "Agent call depth (spans/turn)",
                    "avg":     None,
                    "type":    "integer",
                    "source":  "otel",
                    "status":  "requires_otel",
                    "note":    "Agent spans per turn — detects runaway delegation chains.",
                    "formula": "count(rai.agent.* spans) per session / unique_sessions",
                },
                "agent_token_escalation": {
                    "label":  "Agent token escalation",
                    "avg":    None,
                    "type":   "float",
                    "source": "eval_metadata",
                    "status": "requires_baseline",
                    "note":   "Per-agent tokens vs rolling baseline — catches unexpected work.",
                    "formula": "agent_tokens / rolling_avg_tokens > escalation_threshold",
                },
                "unsanctioned_a2a_flag": {
                    "label":        "Unsanctioned A2A flag",
                    "flagged":      a2a_unsanctioned,
                    "total":        len(a2a_records),
                    "flag_rate_pct": round(a2a_unsanctioned / len(a2a_records) * 100, 1) if a2a_records else 0,
                    "type":         "boolean",
                    "source":       "policy_engine",
                    "status":       "available" if a2a_records else "no_a2a_traffic",
                    "note":         "A2A eval for an agent absent from the declared agent registry.",
                    "formula":      "Σ(a2a evals where agent_name ∉ declared_agents)",
                },
            },
        }

    @router.get("/v1/metrics/dim-breakdown")
    def dim_breakdown(
        app_id:       Optional[str] = Query(None,  description="Filter to a specific app"),
        window_hours: int           = Query(24,    ge=1, le=720,
                                           description="Rolling window in hours (default 24)"),
        db: Session = Depends(get_db),
    ):
        """
        Fast per-dimension breakdown using a single SQL GROUP BY query.

        Returns category-level and dimension-level violation stats with
        distributions by agent_id, content role, and function_name.

        Unlike /v1/metrics/tier1 (which fetches N records into Python),
        this endpoint pushes all aggregation to SQLite — one query,
        sub-50 ms regardless of audit table size.

        Design note: the endpoint shape is stable; the backing query can
        be replaced by a pre-aggregated table (dashboard_dim_hourly_agg)
        in a future migration with no UI changes.

        Response shape:
          {
            "window_hours": 24,
            "app_id": null,
            "total_dim_evals": 1500,
            "a2a_metrics": { ... },
            "categories": {
              "safety": {
                "label": "Safety",
                "total": 150, "flagged": 18, "violation_rate_pct": 12.0,
                "dimensions": {
                  "hate_speech": {
                    "display_name": "Hate Speech",
                    "total": 50, "flagged": 12, "violation_rate_pct": 24.0,
                    "avg_score": 5.2,
                    "by_agent":    {"finapp-agent": 8, "llm-agent": 4},
                    "by_role":     {"user_prompt": 10, "llm_response": 2},
                    "by_function": {"transfer_funds": 8}
                  }
                }
              }
            }
          }
        """
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import text

        since_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        # SQLite stores timestamps as naive ISO strings — strip tzinfo for comparison
        since_str = since_dt.replace(tzinfo=None).isoformat(timespec="seconds")

        params: dict = {"since": since_str}
        app_clause = ""      # used in main JOIN query (table aliased as 'a')
        a2a_app_clause = ""  # used in A2A standalone query (no alias)
        if app_id:
            app_clause     = "AND a.app_id = :app_id"
            a2a_app_clause = "AND app_id = :app_id"
            params["app_id"] = app_id

        # ── Single aggregated query — one row per (dim, role, agent, function) ──
        sql = text(f"""
            SELECT
                COALESCE(ced.category, 'other')                          AS category,
                d.dimension_id,
                COUNT(*)                                                  AS total_evals,
                SUM(CASE WHEN d.is_violation = 1
                          OR  COALESCE(d.score, 0) >= 7
                     THEN 1 ELSE 0 END)                                  AS flagged,
                ROUND(AVG(CAST(d.score AS FLOAT)), 2)                    AS avg_score,
                COALESCE(a.role, 'unknown')                              AS role,
                COALESCE(json_extract(a.metadata_json, '$.agent_name'),
                         a.agent_id,
                         '_unknown')                                     AS agent_id,
                COALESCE(json_extract(a.metadata_json, '$.function_name'),
                         '_none')                                        AS function_name
            FROM audit_evaluation_dimensions d
            JOIN audit_evaluation_audit      a   ON d.evaluation_audit_id = a.id
            LEFT JOIN config_evaluation_dimensions ced ON d.dimension_id  = ced.dimension_id
            WHERE a.timestamp >= :since
            {app_clause}
            GROUP BY d.dimension_id, a.role, a.agent_id, function_name
            ORDER BY d.dimension_id
        """)

        try:
            rows = db.execute(sql, params).fetchall()
        except Exception:
            logger.exception("[dim-breakdown] aggregation query failed")
            rows = []

        # ── Dim metadata for display_name and status ─────────────────────────
        _meta: dict = {}
        try:
            for _d in db.query(EvaluationDimension).all():
                _meta[_d.dimension_id] = {
                    "display_name": _d.display_name or _d.dimension_id.replace("_", " ").title(),
                    "category":     _d.category or "other",
                    "status":       _d.status or "active",
                }
        except Exception:
            pass

        # ── Assemble per-dim aggregates from GROUP BY rows ────────────────────
        _dims: dict = {}
        for row in rows:
            did = row.dimension_id
            cat = (_meta.get(did, {}).get("category") or row.category or "other").lower()
            if did not in _dims:
                _dims[did] = {
                    "category":     cat,
                    "display_name": _meta.get(did, {}).get("display_name") or did.replace("_", " ").title(),
                    "status":       _meta.get(did, {}).get("status", "active"),
                    "total": 0, "flagged": 0, "score_sum": 0.0, "score_n": 0,
                    "by_agent": {}, "by_role": {}, "by_function": {},
                }
            _d = _dims[did]
            _d["total"]   += row.total_evals
            _d["flagged"] += row.flagged
            if row.avg_score is not None:
                _d["score_sum"] += row.avg_score * row.total_evals
                _d["score_n"]   += row.total_evals

            # Distributions — count FLAGGED occurrences
            _fv = int(row.flagged)
            _rl = row.role
            _ag = row.agent_id
            _fn = row.function_name

            if _rl and _rl != "unknown":
                _d["by_role"][_rl] = _d["by_role"].get(_rl, 0) + _fv

            # ── Actor attribution ──────────────────────────────────────────────
            # user_prompt violations are INTERCEPTIONS — the evaluation fired
            # before any agent took action.  Attribute to the synthetic actor
            # "__user_input__" so the UI can display "User Input" distinctly from
            # agents.  tool_call, llm_response, and agent_to_agent violations ARE
            # agent-attributable — the agent chose to act and should own the flag.
            # Missing agent_id on an agent-actioned role = pipeline attribution gap.
            if _fv > 0:
                if _rl == "user_prompt":
                    _d["by_agent"]["__user_input__"] = _d["by_agent"].get("__user_input__", 0) + _fv
                elif _ag and _ag not in ("_unknown",):
                    _d["by_agent"][_ag] = _d["by_agent"].get(_ag, 0) + _fv
                else:
                    # Agent-actioned role with no agent_id — pipeline gap
                    _d["by_agent"]["__unattributed__"] = _d["by_agent"].get("__unattributed__", 0) + _fv

            if _fn and _fn not in ("_none",):
                _d["by_function"][_fn] = _d["by_function"].get(_fn, 0) + _fv

        # ── Build category-level structure ────────────────────────────────────
        # CAT_ORDER follows the RAI pillar taxonomy:
        #   Safety · Security · Privacy · Compliance · Fairness · Transparency
        # Operational categories (corporate_governance, prompt_filter,
        # session_trackers) are returned in full but excluded from the
        # T1 metrics display client-side.
        CAT_ORDER = [
            "safety", "security", "privacy",
            "compliance", "fairness", "transparency",
            "corporate_governance", "prompt_filter", "session_trackers", "other",
        ]
        CAT_LABELS = {
            "safety":               "Safety",
            "security":             "Security",
            "privacy":              "Privacy",
            "compliance":           "Compliance",
            "fairness":             "Fairness",
            "transparency":         "Transparency",
            "corporate_governance": "Corporate Governance",
            "prompt_filter":        "Prompt Filter",
            "session_trackers":     "Session Trackers",
            "other":                "Other",
        }

        _cats: dict = {}
        for did, _d in _dims.items():
            cat = _d["category"]
            if cat not in _cats:
                _cats[cat] = {
                    "label":   CAT_LABELS.get(cat, cat.replace("_", " ").title()),
                    "total":   0,
                    "flagged": 0,
                    "dimensions": {},
                }
            _cats[cat]["total"]   += _d["total"]
            _cats[cat]["flagged"] += _d["flagged"]

            _avg = round(_d["score_sum"] / _d["score_n"], 2) if _d["score_n"] else None
            _vr  = round(_d["flagged"] / _d["total"] * 100, 1) if _d["total"] else 0.0
            _cats[cat]["dimensions"][did] = {
                "display_name":       _d["display_name"],
                "status":             _d["status"],
                "total":              _d["total"],
                "flagged":            _d["flagged"],
                "violation_rate_pct": _vr,
                "avg_score":          _avg,
                # Top-10 per distribution bucket, sorted by violation count desc
                "by_agent":    dict(sorted(_d["by_agent"].items(),    key=lambda x: -x[1])[:10]),
                "by_role":     dict(sorted(_d["by_role"].items(),     key=lambda x: -x[1])),
                "by_function": dict(sorted(_d["by_function"].items(), key=lambda x: -x[1])[:10]),
            }

        # Sort dims within each category by flagged desc
        for cat_data in _cats.values():
            cat_data["violation_rate_pct"] = (
                round(cat_data["flagged"] / cat_data["total"] * 100, 1)
                if cat_data["total"] else 0.0
            )
            cat_data["dimensions"] = dict(
                sorted(cat_data["dimensions"].items(), key=lambda x: -x[1]["flagged"])
            )

        # Order categories
        _ordered = {c: _cats[c] for c in CAT_ORDER if c in _cats}
        _ordered.update({c: _cats[c] for c in _cats if c not in CAT_ORDER})

        # ── A2A aggregate metrics (from audit_evaluation_audit directly) ───────
        # NOTE: uses a2a_app_clause (no table alias) — app_clause uses alias 'a'
        # which is only defined in the main JOIN query above.
        # agent count prefers metadata.agent_name over agent_id column (same as main query).
        _a2a_sql = text(f"""
            SELECT
                COUNT(*)                                                AS total,
                SUM(CASE WHEN verdict IN ('COERCIVE','NORMATIVE')
                     THEN 1 ELSE 0 END)                                 AS violations,
                COUNT(DISTINCT session_id)                              AS sessions,
                COUNT(DISTINCT
                    COALESCE(json_extract(metadata_json, '$.agent_name'),
                             agent_id))                                 AS agents
            FROM audit_evaluation_audit
            WHERE role = 'agent_to_agent'
              AND timestamp >= :since
            {a2a_app_clause}
        """)
        _a2a: dict = {}
        try:
            _r = db.execute(_a2a_sql, params).fetchone()
            if _r and _r.total:
                _a2a = {
                    "total":           _r.total,
                    "violations":      _r.violations,
                    "violation_rate_pct": round(_r.violations / _r.total * 100, 1),
                    "sessions":        _r.sessions,
                    "avg_per_session": round(_r.total / _r.sessions, 2) if _r.sessions else None,
                    "agents":          _r.agents,
                }
        except Exception:
            logger.exception("[dim-breakdown] A2A aggregate query failed")

        # ── A2A per-dimension breakdown ────────────────────────────────────────
        # Shows which specific dimensions are being flagged on agent_to_agent hops.
        # This surfaces the "not scored" gap: A2A verdict may come from session
        # escalation (no dimension flags) or from active dimension evaluations.
        _a2a_dim_sql = text(f"""
            SELECT
                d.dimension_id,
                COALESCE(ced.category, 'other')   AS category,
                COUNT(*)                           AS total,
                SUM(d.is_violation)                AS flagged,
                ROUND(AVG(CAST(d.score AS FLOAT)),2) AS avg_score
            FROM audit_evaluation_dimensions d
            JOIN audit_evaluation_audit      a   ON d.evaluation_audit_id = a.id
            LEFT JOIN config_evaluation_dimensions ced ON d.dimension_id = ced.dimension_id
            WHERE a.role = 'agent_to_agent'
              AND a.timestamp >= :since
            {a2a_app_clause}
            GROUP BY d.dimension_id
            ORDER BY flagged DESC, total DESC
        """)
        _a2a_dims: list = []
        try:
            _a2a_dim_rows = db.execute(_a2a_dim_sql, params).fetchall()
            for _dr in _a2a_dim_rows:
                _dm = _meta.get(_dr.dimension_id, {})
                _a2a_dims.append({
                    "dimension_id": _dr.dimension_id,
                    "display_name": _dm.get("display_name") or _dr.dimension_id.replace("_", " ").title(),
                    "category":     _dr.category,
                    "total":        _dr.total,
                    "flagged":      int(_dr.flagged or 0),
                    "avg_score":    _dr.avg_score,
                    "violation_rate_pct": round(int(_dr.flagged or 0) / _dr.total * 100, 1) if _dr.total else 0.0,
                })
        except Exception:
            logger.exception("[dim-breakdown] A2A dimension breakdown query failed")
        if _a2a:
            _a2a["dimensions"] = _a2a_dims

        return {
            "window_hours":    window_hours,
            "app_id":          app_id,
            "total_dim_evals": sum(_d["total"] for _d in _dims.values()),
            "a2a_metrics":     _a2a,
            "categories":      _ordered,
        }

    @router.get("/v1/metrics/apps-summary")
    def apps_metrics_summary(
        window_minutes: Optional[int] = Query(None, ge=1, le=10080,
                                              description="Only include records from the last N minutes."),
        limit: int = Query(2000, ge=10, le=10000),
        db: Session = Depends(get_db),
    ):
        """
        Return per-app verdict counts from the PAP SQLite database.

        Uses a direct GROUP BY query — no record hydration needed.
        Persistent across PE restarts and log rotation.

        Response shape:
          {
            "apps": [
              {
                "app_id": "mybank",
                "total": 42,
                "permissive": 30,
                "normative": 8,
                "coercive": 4
              },
              ...
            ]
          }
        """
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import text

        params: dict = {"limit": limit}
        _conds: list[str] = []
        if window_minutes is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
            _conds.append("timestamp >= :since")
            params["since"] = cutoff.replace(tzinfo=None).isoformat(timespec="seconds")
        # Exclude visualization-only scopes (trust-platform) — the governance plane is
        # NOT a governed tenant app and must not be counted in the fleet/app list.
        from api.audit_db import _VIZ_APPS as _VA
        for _i, _va in enumerate(_VA):
            _conds.append(f"COALESCE(app_id,'') != :viz_{_i}")
            params[f"viz_{_i}"] = _va
        time_filter = ("WHERE " + " AND ".join(_conds)) if _conds else ""

        sql = text(f"""
            SELECT
                COALESCE(app_id, 'unknown')                            AS app_id,
                COUNT(*)                                               AS total,
                SUM(CASE WHEN verdict = 'PERMISSIVE' THEN 1 ELSE 0 END) AS permissive,
                SUM(CASE WHEN verdict = 'NORMATIVE'  THEN 1 ELSE 0 END) AS normative,
                SUM(CASE WHEN verdict = 'COERCIVE'   THEN 1 ELSE 0 END) AS coercive
            FROM (
                SELECT app_id, verdict
                FROM audit_evaluation_audit
                {time_filter}
                ORDER BY timestamp DESC
                LIMIT :limit
            ) sub
            GROUP BY app_id
            ORDER BY total DESC
        """)

        rows = db.execute(sql, params).fetchall()

        apps: list[dict] = []
        fleet_total      = 0
        fleet_permissive = 0
        fleet_normative  = 0
        fleet_coercive   = 0

        for r in rows:
            total    = r.total    or 0
            coercive = r.coercive or 0
            normative  = r.normative  or 0
            permissive = r.permissive or 0
            coercive_rate = round(coercive / total, 4) if total > 0 else 0.0

            # Risk level thresholds — tunable without code change (just edit these values)
            # HIGH ≥ 10 % coercive: app flagged "at risk" in the Enterprise Trust dashboard
            if coercive_rate >= 0.10:
                risk_level = "HIGH"
            elif coercive_rate >= 0.05:
                risk_level = "MEDIUM"
            elif coercive_rate > 0:
                risk_level = "LOW"
            else:
                risk_level = "OK"

            apps.append({
                "app_id":        r.app_id,
                "total":         total,
                "permissive":    permissive,
                "normative":     normative,
                "coercive":      coercive,
                "coercive_rate": coercive_rate,              # float 0.0–1.0 (never a summed %)
                "trust_score":   round(1 - coercive_rate, 4),# positive framing: 1 − coercive_rate
                "risk_level":    risk_level,                  # HIGH / MEDIUM / LOW / OK
            })
            fleet_total      += total
            fleet_permissive += permissive
            fleet_normative  += normative
            fleet_coercive   += coercive

        # ── Always include ONBOARDED apps (config_apps), even with no activity ────
        # App Scope / Cockpit should list every onboarded app, not only those with
        # evaluations in the current window — so a freshly onboarded app (e.g. MyBank
        # before its first request) is visible. Idle apps get zero counts (trust 1.0,
        # risk OK) and don't affect the fleet verdict sums (they add 0).
        try:
            _seen = {a["app_id"] for a in apps}
            for (_oid,) in db.execute(text(
                "SELECT app_id FROM config_apps WHERE active = 1 ORDER BY app_id"
            )).fetchall():
                if _oid and _oid not in _seen:
                    apps.append({
                        "app_id": _oid, "total": 0,
                        "permissive": 0, "normative": 0, "coercive": 0,
                        "coercive_rate": 0.0, "trust_score": 1.0, "risk_level": "OK",
                    })
        except Exception:
            pass  # config_apps unavailable → fall back to the activity-only list

        # Fleet-level aggregates — derived from sums, never an average of per-app rates
        fleet_coercive_rate = round(fleet_coercive / fleet_total, 4) if fleet_total > 0 else 0.0
        fleet_trust_score   = round(1 - fleet_coercive_rate, 4)
        # "At risk" = only HIGH (≥ 10 % coercive); MEDIUM apps are monitored, not yet critical
        apps_at_risk = sum(1 for a in apps if a["risk_level"] == "HIGH")

        return {
            "fleet": {
                "total_evaluations": fleet_total,
                "permissive":        fleet_permissive,
                "normative":         fleet_normative,
                "coercive":          fleet_coercive,
                "coercive_rate":     fleet_coercive_rate,  # SUM(coercive)/SUM(total)
                "trust_score":       fleet_trust_score,    # positive framing: 1 − coercive_rate
                "apps_at_risk":      apps_at_risk,          # count of HIGH-risk apps (≥ 10%)
                "app_count":         len(apps),
            },
            "apps": apps,
        }

    # ================================================================== #
    #  PROXY — UI calls these to avoid cross-origin issues               #
    # ================================================================== #

    @router.get("/v1/proxy/config")
    def proxy_config():
        """Return PE runtime configuration (Tier B parameters)."""
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/internal/config", timeout=3.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.get("/v1/proxy/packs")
    def proxy_packs():
        """Return full pack definitions from the Policy Engine for the PAP Pack Inspector."""
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/internal/packs", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(
                status_code=503,
                content={"pack_engine_active": False, "packs": [], "error": str(e)},
            )

    # ── Dimension catalogue (PAP DB — authoritative for role assignments) ──────

    @router.get("/v1/dimensions")
    def list_dimensions(db: Session = Depends(get_db)):
        """
        Return all evaluation dimensions from the PAP database.

        This is the authoritative source for operator-configured role assignments
        (supported_roles).  The GS evaluations.yaml provides defaults at seed time;
        operator edits via PATCH /v1/evaluators/{dimension_id} are persisted here
        and survive GS restarts.

        Used by the Evaluations tab to show editable interception-point assignments.
        """
        dims = db.query(EvaluationDimension).order_by(
            EvaluationDimension.category, EvaluationDimension.dimension_id
        ).all()
        return {
            "total":      len(dims),
            "dimensions": [d.to_dict() for d in dims],
        }

    @router.patch("/v1/evaluators/{dimension_id}", status_code=200)
    def update_evaluator_config(
        dimension_id: str,
        body:         EvaluatorRolesUpdate,
        db:           Session = Depends(get_db),
        actor:        str     = Depends(_get_actor),
    ):
        """
        Update the interception-point assignments and/or activation status of an
        evaluation dimension.

        Fields:
          supported_roles — list of content roles this dimension runs on.
                            Subset of: user_prompt, llm_response, tool_call, agent_to_agent
          status          — "active" (enforces policy thresholds) or
                            "deferred" (scores only — no verdict contribution)

        Changes are persisted in the PAP database and survive GS restarts.
        An audit log entry is written for every change.
        """
        dim = (
            db.query(EvaluationDimension)
            .filter(EvaluationDimension.dimension_id == dimension_id)
            .first()
        )
        if not dim:
            raise HTTPException(
                status_code=404,
                detail=f"Dimension '{dimension_id}' not found in PAP catalogue. "
                       f"Run seed or check the dimension ID.",
            )

        changes: dict = {}

        if body.supported_roles is not None:
            old_roles = sorted(dim.supported_roles or [])
            new_roles = sorted(body.supported_roles)
            if old_roles != new_roles:
                dim.supported_roles = new_roles
                changes["supported_roles"] = {"from": old_roles, "to": new_roles}

        if body.status is not None and body.status != dim.status:
            changes["status"] = {"from": dim.status, "to": body.status}
            dim.status = body.status

        if body.provider is not None and body.provider != dim.provider:
            # ── Provider capability + availability gate ───────────────────────
            prov_obj = db.query(Provider).filter(Provider.provider_id == body.provider).first()
            if not prov_obj:
                raise HTTPException(
                    status_code=404,
                    detail=f"Provider '{body.provider}' not found in registry.",
                )
            # Derive operational state (same logic as Provider.to_dict())
            cred_available = bool(os.getenv(prov_obj.api_key_env_var)) if prov_obj.api_key_env_var else True
            if not prov_obj.active or not cred_available:
                state = "unavailable" if not cred_available else "disabled"
                hint  = (f"Set the {prov_obj.api_key_env_var} environment variable."
                         if not cred_available else "Enable it in the Provider Registry first.")
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot assign '{body.provider}': provider is {state}. {hint} "
                        f"Mock provider will be used as fallback until this is resolved."
                    ),
                )
            # Capability check 1: if provider declares supported_dims, dimension must be in the list
            supported = prov_obj.supported_dims or []
            if supported and dimension_id not in supported:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Provider '{body.provider}' does not support dimension '{dimension_id}'. "
                        f"Its declared capabilities are: {supported}. "
                        f"Update the provider's capability list first, or choose a different provider."
                    ),
                )
            # Capability check 2: provider must handle all interception roles configured for this dimension.
            # Empty supported_roles on the provider means universal (handles all roles).
            # Empty supported_roles on the dimension means no interception-point restriction.
            prov_roles = prov_obj.supported_roles or []
            dim_roles  = dim.supported_roles or []
            if prov_roles and dim_roles:
                uncovered = [r for r in dim_roles if r not in prov_roles]
                if uncovered:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Provider '{body.provider}' does not handle interception "
                            f"role(s) {uncovered} configured for '{dimension_id}'. "
                            f"Provider supports: {prov_roles}. "
                            f"Adjust the dimension's interception points or choose a compatible provider."
                        ),
                    )
            changes["provider"] = {"from": dim.provider, "to": body.provider}
            dim.provider = body.provider

        if not changes:
            return {"message": "No changes — values already match.", **dim.to_dict()}

        dim.updated_at = datetime.utcnow()

        db.add(PapAuditLog(
            entity_type="evaluation_dimension",
            entity_id=dimension_id,
            app_id=None,
            action="UPDATE",
            actor=actor,
            old_values=json.dumps({k: v["from"] for k, v in changes.items()}),
            new_values=json.dumps({k: v["to"]   for k, v in changes.items()}),
        ))

        try:
            db.commit()
            db.refresh(dim)
        except Exception as e:
            db.rollback()
            logger.exception(f"[PAP] update_evaluator_config: DB error for {dimension_id}")
            raise HTTPException(status_code=500, detail=str(e))

        logger.info(
            f"[PAP] Dimension '{dimension_id}' updated by {actor!r}: {changes}"
        )
        return dim.to_dict()

    # ── Provider registry ──────────────────────────────────────────────────────

    @router.get("/v1/providers")
    def list_providers(
        active_only: bool = Query(False, description="When true, return only active providers"),
        db: Session = Depends(get_db),
    ):
        """
        Return providers from config_providers.

        Default: returns ALL providers (active and inactive) so the Provider Registry
        panel can show disabled providers and let operators re-enable them.
        Pass ?active_only=true to get only active providers (dimension dropdown usage).

        Each entry includes health_status and credential_available so the UI can
        render health / credential badges and warn on inactive/uncredentialed selection.
        """
        q = db.query(Provider)
        if active_only:
            q = q.filter(Provider.active == True)
        providers = q.order_by(Provider.provider_id).all()
        return {
            "total":     len(providers),
            "providers": [p.to_dict() for p in providers],
        }

    @router.patch("/v1/providers/{provider_id}")
    def toggle_provider(
        provider_id: str,
        body: dict,
        db: Session = Depends(get_db),
    ):
        """
        Toggle provider active flag (and optionally other mutable fields).

        Accepted body fields:
          active  (bool)  — enable / disable the provider
          fail_mode (str) — "fail_open" | "fail_closed"

        Writes an audit log entry on change.
        """
        prov = db.query(Provider).filter(Provider.provider_id == provider_id).first()
        if not prov:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")

        old_values: dict = {}
        new_values: dict = {}
        action_parts: list = []

        if "active" in body and isinstance(body["active"], bool):
            if prov.active != body["active"]:
                old_values["active"] = prov.active
                new_values["active"] = body["active"]
                prov.active = body["active"]
                action_parts.append("ENABLE" if body["active"] else "DISABLE")

        if "fail_mode" in body and body["fail_mode"] in ("fail_open", "fail_closed"):
            if prov.fail_mode != body["fail_mode"]:
                old_values["fail_mode"] = prov.fail_mode
                new_values["fail_mode"] = body["fail_mode"]
                prov.fail_mode = body["fail_mode"]
                action_parts.append("UPDATE")

        if "supported_dims" in body and isinstance(body["supported_dims"], list):
            new_dims = [str(d).strip() for d in body["supported_dims"] if str(d).strip()]
            if sorted(prov.supported_dims or []) != sorted(new_dims):
                old_values["supported_dims"] = prov.supported_dims or []
                new_values["supported_dims"] = new_dims
                prov.supported_dims = new_dims
                action_parts.append("UPDATE")

        if old_values:
            action = action_parts[0] if action_parts else "UPDATE"
            log = PapAuditLog(
                entity_type="provider",
                entity_id=provider_id,
                action=action,
                actor="pap-admin",
                old_values=json.dumps(old_values),
                new_values=json.dumps(new_values),
                notes=f"Provider '{provider_id}' {action.lower()}d via PAP UI",
            )
            db.add(log)
            db.commit()
            db.refresh(prov)

        return prov.to_dict()

    # ── Trust scoring formula (enterprise-default weights) ────────────────────

    _DEFAULT_WEIGHTS = {
        "safety": 0.30, "security": 0.25, "compliance": 0.20,
        "operational": 0.10, "quality": 0.15,
    }

    @router.get("/v1/platform/trust-weights")
    def get_trust_weights(db: Session = Depends(get_db)):
        """
        Return the enterprise-default app trust scoring formula weights.
        NULL app_id row = enterprise default (applied when no app-specific row exists).
        """
        row = db.query(AppTrustWeights).filter(AppTrustWeights.app_id == None).first()
        if not row:
            return {
                "enterprise_id": None, "app_id": None,
                "weights": dict(_DEFAULT_WEIGHTS),
                "alert_thresholds": {"1h": 0.05, "6h": 0.10, "24h": 0.15},
                "_source": "hardcoded_defaults",
            }
        result = row.to_dict()
        result["_source"] = "database"
        return result

    @router.put("/v1/platform/trust-weights")
    def update_trust_weights(
        body:  dict,
        db:    Session = Depends(get_db),
        actor: str     = Depends(_get_actor),
    ):
        """
        Upsert the enterprise-default trust scoring formula weights.

        Body: { "weights": { "safety": 0.30, "security": 0.25, "compliance": 0.20,
                              "operational": 0.10, "quality": 0.15 } }
        Weights must sum to 1.0 (±0.01 tolerance for float rounding).
        """
        weights = body.get("weights", {})
        required = {"safety", "security", "compliance", "operational", "quality"}
        missing = required - set(weights.keys())
        if missing:
            raise HTTPException(status_code=422, detail=f"Missing weight keys: {sorted(missing)}")

        for k, v in weights.items():
            if k not in required:
                raise HTTPException(status_code=422, detail=f"Unknown weight key '{k}'")
            if not isinstance(v, (int, float)) or v < 0 or v > 1:
                raise HTTPException(status_code=422, detail=f"Weight '{k}' must be 0.0–1.0, got {v}")

        total = sum(weights[k] for k in required)
        if abs(total - 1.0) > 0.011:
            raise HTTPException(status_code=422,
                detail=f"Weights must sum to 1.0 (got {total:.4f}). Adjust values so they total 100%.")

        row = db.query(AppTrustWeights).filter(AppTrustWeights.app_id == None).first()
        if not row:
            row = AppTrustWeights(app_id=None)
            db.add(row)

        old_w = {k: getattr(row, f"weight_{k}", _DEFAULT_WEIGHTS[k]) for k in required}
        row.weight_safety      = weights["safety"]
        row.weight_security    = weights["security"]
        row.weight_compliance  = weights["compliance"]
        row.weight_operational = weights["operational"]
        row.weight_quality     = weights["quality"]
        row.updated_at         = datetime.utcnow()

        db.add(PapAuditLog(
            entity_type="trust_weights",
            entity_id="enterprise_default",
            action="UPDATE",
            actor=actor,
            old_values=json.dumps(old_w),
            new_values=json.dumps({k: weights[k] for k in required}),
            notes="Trust scoring formula updated via Platform Rules tab",
        ))
        db.commit()
        db.refresh(row)
        result = row.to_dict()
        result["_source"] = "database"
        logger.info(f"[PAP] Trust weights updated by {actor!r}: {old_w} → {weights}")
        return result

    @router.get("/v1/proxy/evaluations")
    def proxy_evaluations():
        """Forward GET /v1/evaluations to the Guardrail Service."""
        try:
            r = httpx.get(f"{GUARDRAIL_URL}/v1/evaluations", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.post("/v1/proxy/packs")
    def proxy_create_pack(body: dict):
        """Create a new policy pack via Policy Engine Pack Manager."""
        try:
            r = httpx.post(f"{POLICY_ENGINE_URL}/internal/packs", json=body, timeout=10.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.put("/v1/proxy/packs/{pack_id}")
    def proxy_update_pack(pack_id: str, body: dict):
        """Update an existing policy pack via Policy Engine Pack Manager."""
        try:
            r = httpx.put(f"{POLICY_ENGINE_URL}/internal/packs/{pack_id}", json=body, timeout=10.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.delete("/v1/proxy/packs/{pack_id}")
    def proxy_delete_pack(pack_id: str):
        """Delete a policy pack via Policy Engine Pack Manager."""
        try:
            r = httpx.delete(f"{POLICY_ENGINE_URL}/internal/packs/{pack_id}", timeout=10.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.patch("/v1/proxy/packs/{pack_id}/toggle")
    def proxy_toggle_pack(pack_id: str):
        """Toggle active status of a policy pack (enabled ↔ disabled)."""
        try:
            r = httpx.patch(f"{POLICY_ENGINE_URL}/internal/packs/{pack_id}/toggle", timeout=10.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    # ── App Stack proxy endpoints ─────────────────────────────────────────────

    @router.get("/v1/proxy/app-stacks")
    def proxy_list_app_stacks():
        """List all app stack definitions from the Policy Engine."""
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/internal/app-stacks", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.get("/v1/proxy/app-stacks/{app_id}")
    def proxy_get_app_stack(app_id: str):
        """Return the stack definition for one app."""
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.post("/v1/proxy/app-stacks/{app_id}/packs/{pack_id}")
    def proxy_assign_pack(app_id: str, pack_id: str, body: dict = {}):
        """Assign a pack to an app (or update active flag)."""
        try:
            r = httpx.post(
                f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}/packs/{pack_id}",
                json=body, timeout=10.0,
            )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.delete("/v1/proxy/app-stacks/{app_id}/packs/{pack_id}")
    def proxy_remove_pack(app_id: str, pack_id: str):
        """Remove a pack assignment from an app."""
        try:
            r = httpx.delete(
                f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}/packs/{pack_id}",
                timeout=10.0,
            )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.patch("/v1/proxy/app-stacks/{app_id}/packs/{pack_id}/toggle")
    def proxy_toggle_app_pack(app_id: str, pack_id: str):
        """Toggle the active flag for a pack assignment."""
        try:
            r = httpx.patch(
                f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}/packs/{pack_id}/toggle",
                timeout=10.0,
            )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.put("/v1/proxy/app-stacks/{app_id}/packs/{pack_id}/overrides")
    def proxy_set_overrides(app_id: str, pack_id: str, body: dict):
        """Replace dimension overrides for a pack+app."""
        try:
            r = httpx.put(
                f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}/packs/{pack_id}/overrides",
                json=body, timeout=10.0,
            )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.delete("/v1/proxy/app-stacks/{app_id}/packs/{pack_id}/overrides")
    def proxy_clear_overrides(app_id: str, pack_id: str):
        """Clear all overrides for a pack+app."""
        try:
            r = httpx.delete(
                f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}/packs/{pack_id}/overrides",
                timeout=10.0,
            )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.post("/v1/proxy/app-stacks/{app_id}/packs/{pack_id}/propagate")
    def proxy_propagate_overrides(app_id: str, pack_id: str, body: dict):
        """Propagate pack overrides from one app to others."""
        try:
            r = httpx.post(
                f"{POLICY_ENGINE_URL}/internal/app-stacks/{app_id}/packs/{pack_id}/propagate",
                json=body, timeout=10.0,
            )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.get("/v1/proxy/app-trust")
    def proxy_app_trust():
        """Return per-app trust scores from the Policy Engine AppTrustTracker.

        Timeout is intentionally short (1 s) — this endpoint is called outside
        the main dashboard render path so a slow PE never blocks the UI.
        """
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/internal/app-trust", timeout=1.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.delete("/v1/proxy/app-trust/{app_id}")
    def proxy_reset_app_trust(app_id: str):
        """Reset an app's trust score to 1.0 via the Policy Engine."""
        try:
            r = httpx.delete(f"{POLICY_ENGINE_URL}/internal/app-trust/{app_id}", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.get("/v1/proxy/services")
    def proxy_services():
        """Return health status of Guardrail Service and Policy Engine."""
        results = {}

        for name, url in [("guardrail_service", GUARDRAIL_URL), ("policy_engine", POLICY_ENGINE_URL)]:
            try:
                r = httpx.get(f"{url}/v1/health", timeout=3.0)
                body = r.json()
                results[name] = {"status": body.get("status", "unknown"), "url": url}
            except Exception as e:
                results[name] = {"status": "unreachable", "url": url, "error": str(e)}

        return results

    @router.get("/v1/proxy/platform-health")
    def proxy_platform_health():
        """Aggregated liveness for the four trust services + the PE fail mode.

        Pinged server-side (no browser CORS) so the dashboard can show a row of
        liveness dots and the current governance fail-open/closed posture.

        Each service exposes a different health path:
          - Guardrail / Policy Engine : /v1/health
          - Sentinel / Coordinator    : /health
        """
        targets = [
            ("policy_engine", "Policy Engine", POLICY_ENGINE_URL, "/v1/health"),
            ("guardrail",     "Guardrail",     GUARDRAIL_URL,     "/v1/health"),
            ("sentinel",      "Sentinel",      SENTINEL_URL,      "/health"),
            ("coordinator",   "Coordinator",   COORDINATOR_URL,   "/health"),
        ]
        services = {}
        for key, label, url, path in targets:
            try:
                r = httpx.get(f"{url}{path}", timeout=2.0)
                services[key] = {"label": label, "up": r.status_code == 200, "url": url}
            except Exception:
                services[key] = {"label": label, "up": False, "url": url}

        # Fail mode is a PE configuration ("open" → PERMISSIVE on dependency
        # failure, "closed" → COERCIVE).  Read it from the PE config snapshot.
        fail_mode = "unknown"
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/internal/config", timeout=2.0)
            if r.status_code == 200:
                fail_mode = (r.json().get("fail_mode") or "unknown").lower()
        except Exception:
            pass

        # The governance pipeline is effectively BYPASSED when the Guardrail is
        # unreachable and the PE is configured fail-open: every verdict degrades
        # to PERMISSIVE, so HITL/blocks silently stop firing.  Surface that.
        gs_up = services.get("guardrail", {}).get("up", False)
        bypassing = (not gs_up) and (fail_mode == "open")

        return {
            "services":   services,
            "fail_mode":  fail_mode,
            "bypassing":  bypassing,
        }

    # ================================================================== #
    #  MOCK EVALUATOR CRUD                                               #
    #  Read/write guardrail-service/config/mock_scenarios.yaml signals   #
    # ================================================================== #

    _MOCK_SCENARIOS_PATH = Path(__file__).parent.parent.parent / "guardrail-service" / "config" / "mock_scenarios.yaml"

    @router.get("/v1/proxy/mock-evaluators")
    def get_mock_evaluators():
        """Return current keyword signals and scenarios from mock_scenarios.yaml."""
        import yaml
        try:
            with open(_MOCK_SCENARIOS_PATH, "r") as f:
                data = yaml.safe_load(f)
            return {
                "signals":   data.get("signals", {}),
                "bands":     data.get("bands", {}),
                "scenarios": data.get("scenarios", {}),
            }
        except FileNotFoundError:
            # Optional dev-only scenarios file. The mock provider ships with built-in
            # keyword signals, so its absence is normal — return empty so the UI
            # renders "no signals defined" cleanly instead of throwing a 404.
            return {"signals": {}, "bands": {}, "scenarios": {}}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/v1/proxy/mock-evaluators/{dimension}")
    def update_mock_evaluator_keywords(dimension: str, body: dict):
        """
        Update keyword list for a single dimension signal.
        Body: { "keywords": ["word1", "word2", ...] }
        """
        import yaml
        try:
            with open(_MOCK_SCENARIOS_PATH, "r") as f:
                data = yaml.safe_load(f)

            signals = data.get("signals", {})
            if dimension not in signals:
                raise HTTPException(status_code=404, detail=f"Dimension '{dimension}' not found in signals")

            keywords = body.get("keywords")
            if keywords is None or not isinstance(keywords, list):
                raise HTTPException(status_code=422, detail="Body must contain 'keywords' list")

            signals[dimension]["keywords"] = [str(k) for k in keywords]
            data["signals"] = signals

            with open(_MOCK_SCENARIOS_PATH, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            # Hot-reload GS mock signals so changes take effect immediately
            try:
                httpx.post(f"{GUARDRAIL_URL}/internal/mock-signals/reload", timeout=3.0)
            except Exception as reload_err:
                logger.warning(f"[PAP] mock-evaluators: GS reload signal failed (non-fatal): {reload_err}")

            logger.info(f"[PAP] mock-evaluators: updated keywords for '{dimension}' ({len(keywords)} keywords)")
            return {"dimension": dimension, "keywords": signals[dimension]["keywords"], "updated": True}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/v1/proxy/mock-signals/reload")
    def proxy_reload_mock_signals():
        """Trigger a hot-reload of mock_scenarios.yaml in the Guardrail Service."""
        try:
            r = httpx.post(f"{GUARDRAIL_URL}/internal/mock-signals/reload", timeout=3.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.put("/v1/proxy/mock-scenarios/{scenario_name}")
    def update_mock_scenario(scenario_name: str, body: dict):
        """
        Update or create a named scenario.
        Body: { "description": "...", "overrides": {"dim": {"score": N, "status": "..."}, ...}, "override_all": {...} }
        """
        import yaml
        try:
            with open(_MOCK_SCENARIOS_PATH, "r") as f:
                data = yaml.safe_load(f)

            scenarios = data.get("scenarios", {})
            scenarios[scenario_name] = body
            data["scenarios"] = scenarios

            with open(_MOCK_SCENARIOS_PATH, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            logger.info(f"[PAP] mock-scenarios: upserted scenario '{scenario_name}'")
            return {"scenario": scenario_name, "updated": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ================================================================== #
    #  INTERNAL — SDK agent topology registration                        #
    #  POST /internal/register-agents                                    #
    #                                                                    #
    #  Called once by GovernedGraph at startup (fire-and-forget thread). #
    #  Upserts declared agent topology into config_app_agents so the     #
    #  dashboard can show declared agents even before eval records exist. #
    # ================================================================== #

    @router.post("/internal/register-agents", status_code=200)
    def register_agents(
        body: dict,
        db:   Session = Depends(get_db),
    ):
        """
        Upsert agent topology for an application.

        Body fields:
          app_id          (str)            — application identifier
          discovered_nodes (list[str])     — all non-internal graph node names
          agent_nodes      (list[str])     — nodes declared in instrument(agent_nodes=...)
          a2a_tools        (dict[str,str]) — {tool_name: target_agent_name}

        One config_app_agents row is written per (app_id, node_name).
        is_declared_agent is True when the node is in agent_nodes or
        is a value in a2a_tools (i.e., it IS an agent boundary, not just a
        routing shim).
        """
        app_id           = body.get("app_id", "")
        discovered_nodes = body.get("discovered_nodes") or []
        agent_nodes      = set(body.get("agent_nodes") or [])
        a2a_tools        = body.get("a2a_tools") or {}

        if not app_id:
            raise HTTPException(status_code=422, detail="app_id is required")

        # All declared agents = agent_nodes ∪ a2a_tools.values()
        declared_names = agent_nodes | set(a2a_tools.values())

        # Tools that route INTO each agent (from a2a_tools)
        agent_incoming_tools: dict = {}
        for tool_name, target_agent in a2a_tools.items():
            agent_incoming_tools.setdefault(target_agent, []).append(tool_name)

        # All reachable A2A targets in this app (for declared agents)
        all_a2a_targets = sorted(set(a2a_tools.values()))

        from datetime import timedelta as _timedelta
        now          = datetime.utcnow()
        # GRACE window: an agent absent from the declaration for longer than this is
        # retired. Must exceed a rolling-deploy duration so an old replica briefly
        # declaring a stale set doesn't flap live agents.
        grace_min    = int(os.getenv("AGENT_RETIRE_GRACE_MIN", "15"))
        grace_cutoff = now - _timedelta(minutes=grace_min)

        upserted    = 0
        added       = []   # brand-new agents
        reinstated  = []   # previously-retired agents that re-appeared

        for node_name in set(list(discovered_nodes) + list(declared_names)):
            is_declared    = node_name in declared_names
            declared_tools = agent_incoming_tools.get(node_name, [])
            new_status     = "active" if is_declared else "observed"

            existing = db.query(AppAgent).filter(
                AppAgent.app_id     == app_id,
                AppAgent.agent_name == node_name,
            ).first()

            if existing is None:
                db.add(AppAgent(
                    app_id            = app_id,
                    agent_name        = node_name,
                    is_declared_agent = is_declared,
                    declared_tools    = declared_tools,
                    a2a_targets       = all_a2a_targets if is_declared else [],
                    all_nodes         = sorted(discovered_nodes),
                    status            = new_status,
                    first_seen_at     = now,
                    last_seen_at      = now,
                    last_declared_at  = now,
                ))
                added.append(node_name)
            else:
                if existing.status == "retired":
                    reinstated.append(node_name)
                # Re-declared → refresh topology + lifecycle (and revive if retired).
                existing.is_declared_agent = is_declared
                existing.declared_tools    = declared_tools
                existing.a2a_targets       = all_a2a_targets if is_declared else []
                existing.all_nodes         = sorted(discovered_nodes)
                existing.last_seen_at      = now
                existing.last_declared_at  = now
                existing.status            = new_status
                existing.retired_at        = None

            upserted += 1

        # ── Reconcile: retire agents removed from the governed app ────────────
        # Still live (active/observed) but not declared within the GRACE window →
        # no longer part of the app. Kept (not deleted) for history + audit
        # attribution of past evals. Agents in THIS declaration were just stamped
        # last_declared_at=now, so they are naturally excluded.
        retired = []
        for ag in db.query(AppAgent).filter(
            AppAgent.app_id == app_id,
            AppAgent.status.in_(("active", "observed")),
            AppAgent.last_declared_at < grace_cutoff,
        ).all():
            ag.status     = "retired"
            ag.retired_at = now
            retired.append(ag.agent_name)

        # ── Topology-drift audit events (auditable add/remove trail) ──────────
        def _drift(name, action, status, note):
            db.add(PapAuditLog(
                entity_type="app_agent", entity_id=name, app_id=app_id,
                action=action, actor="sdk:register-agents",
                new_values=json.dumps({"status": status}), notes=note, timestamp=now,
            ))
        for name in added:
            _drift(name, "AGENT_ADDED",
                   "active" if name in declared_names else "observed",
                   "New agent appeared in the app's declared topology.")
        for name in reinstated:
            _drift(name, "AGENT_REINSTATED",
                   "active" if name in declared_names else "observed",
                   "Previously-retired agent re-appeared in the app's declaration.")
        for name in retired:
            _drift(name, "AGENT_RETIRED", "retired",
                   f"Agent absent from the app's declaration for > {grace_min} min — retired.")

        db.commit()
        logger.info(
            "register-agents: app=%s upserted=%d declared=%d discovered=%d "
            "added=%d reinstated=%d retired=%d",
            app_id, upserted, len(declared_names), len(discovered_nodes),
            len(added), len(reinstated), len(retired),
        )
        return {"status": "ok", "app_id": app_id, "upserted": upserted,
                "added": added, "reinstated": reinstated, "retired": retired}

    # ================================================================== #
    #  AGENT ACTIVITY METRICS                                            #
    #  Aggregates per-agent evaluations, violations, tokens, and cost    #
    #  from audit records where metadata.agent_name is populated.        #
    # ================================================================== #

    @router.get("/v1/metrics/agent-activity")
    def agent_activity(
        limit:          int            = Query(2000, ge=10, le=10000),
        app_id:         Optional[str]  = Query(None, description="Filter to a specific application"),
        window_minutes: Optional[int]  = Query(None, ge=1, le=43200,
                                               description="Restrict to records from the last N minutes."),
        db:             Session        = Depends(get_db),
    ):
        """
        Return per-agent evaluation counts, violation counts, token usage,
        estimated cost, tools called, and session count.

        Sources:
          Observed data  — audit records where metadata.agent_name is set
                           (tool_call, agent_to_agent, llm_response evals).
          Declared data  — config_app_agents rows written by GovernedGraph at
                           startup; shows agents with zero traffic if not yet
                           exercised.

        Filter: only agents that are declared (is_declared_agent=True) OR
        have at least one tool_call or agent_to_agent evaluation are returned.
        Pure routing nodes (only LLM attribution) are excluded.

        Token deduplication: metadata.agent_tokens is a cumulative snapshot.
        We take the maximum per (session_id, agent_name) pair to avoid
        double-counting repeated LLM calls within the same node execution.

        New fields vs. prior version:
          tools_called      — sorted list of unique tool names from tool_call evals
          sessions          — count of distinct session_ids
          is_declared_agent — True if registered via instrument(agent_nodes=...)
          declared_tools    — tools that route into this agent (from a2a_tools)
          a2a_targets       — other agents reachable from this app via A2A
          unsanctioned_a2a  — True if this agent has A2A evals but is NOT declared
          avg_latency_ms    — mean governance eval latency for this agent
          window_minutes    — time window filter (None = all recorded traffic)
        """
        from api.audit_db import load_audit_records
        from datetime import datetime, timezone, timedelta
        window_cutoff = None
        if window_minutes is not None:
            window_cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        all_records = load_audit_records(db, limit=limit, app_id=app_id, since=window_cutoff)

        # agent_name → aggregated stats
        agents: dict = {}
        # (session_id, agent_name) → token snapshot dict
        # Used to deduplicate cumulative token snapshots per session+agent
        token_snapshots: dict = {}

        counted = 0
        for rec in all_records:
            meta = rec.get("metadata") or {}
            # metadata is always a dict from load_audit_records, but guard anyway
            if isinstance(meta, str):
                try:
                    import json as _json
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}

            agent_name = meta.get("agent_name")
            if not agent_name:
                continue

            counted += 1

            # ── Evaluation and violation counts ────────────────────────────
            entry = agents.setdefault(agent_name, {
                "agent_name":           agent_name,
                "evaluations":          0,
                # output_violations: COERCIVE on TOOL_CALL or LLM_RESPONSE
                #   → agent produced or attempted something that violated policy
                "output_violations":    0,
                # input_blocks: COERCIVE on USER_PROMPT or AGENT_TO_AGENT
                #   → governance blocked the request before the agent acted;
                #     the agent itself may have taken corrective action — do NOT
                #     count these against the agent's trust score.
                "input_blocks":         0,
                "violations":           0,   # total COERCIVE (output_violations + input_blocks) kept for backward compat
                "warnings":             0,   # NORMATIVE verdicts
                # violations_by_role: COERCIVE count per content role
                #   → used to compute platform_accountability_score using
                #     per-role weights from trust_accountability ServiceConfig
                "violations_by_role":   {},
                "roles":                set(),
                "tools_called":         set(),   # unique tool names (tool_call evals)
                "sessions":             set(),   # unique session_ids
                "violation_dimensions": {},  # {dimension: count} for COERCIVE records
                "warning_dimensions":   {},  # {dimension: count} for NORMATIVE records
                "_latency_sum":         0.0,
                "_latency_count":       0,
                "total_tokens":         0,
                "prompt_tokens":        0,
                "completion_tokens":    0,
                "cost_usd":             0.0,
            })
            entry["evaluations"] += 1
            verdict = rec.get("verdict", "")
            role    = rec.get("role", "")
            if verdict == "COERCIVE":
                entry["violations"] += 1
                if role in ("tool_call", "llm_response"):
                    entry["output_violations"] += 1
                else:
                    # user_prompt, agent_to_agent — governance intercept, not agent fault
                    entry["input_blocks"] += 1
                # Track per-role for platform_accountability_score computation
                if role:
                    entry["violations_by_role"][role] = (
                        entry["violations_by_role"].get(role, 0) + 1
                    )
            elif verdict == "NORMATIVE":
                entry["warnings"] += 1

            # ── Dimension-level breakdown (score ≥ 4 = MEDIUM+) ───────────
            if verdict in ("COERCIVE", "NORMATIVE"):
                target = "violation_dimensions" if verdict == "COERCIVE" else "warning_dimensions"
                for dr in (rec.get("dimension_results") or []):
                    dim   = dr.get("dimension", "")
                    score = dr.get("score") or 0
                    if dim and score >= 4:
                        entry[target][dim] = entry[target].get(dim, 0) + 1

            role = rec.get("role", "")
            if role:
                entry["roles"].add(role)

            # ── Tools called (from tool_call evals) ───────────────────────
            tool_name = meta.get("tool_name")
            if tool_name and role == "tool_call":
                entry["tools_called"].add(tool_name)

            # ── Session count ──────────────────────────────────────────────
            session_id = rec.get("session_id")
            if session_id:
                entry["sessions"].add(session_id)

            # ── Latency (governance eval round-trip) ──────────────────────
            latency_ms = rec.get("latency_ms")
            if latency_ms and isinstance(latency_ms, (int, float)) and latency_ms > 0:
                entry["_latency_sum"]   += latency_ms
                entry["_latency_count"] += 1

            # ── Token usage (deduplicated per session + agent) ─────────────
            agent_tokens = meta.get("agent_tokens") or {}
            if isinstance(agent_tokens, dict) and agent_tokens.get("total_tokens", 0) > 0:
                snap_key = (session_id or "_no_session", agent_name)
                existing = token_snapshots.get(snap_key, {})
                # Only update if this snapshot is larger (later in the execution)
                if agent_tokens.get("total_tokens", 0) > existing.get("total_tokens", 0):
                    token_snapshots[snap_key] = agent_tokens

        # ── Accumulate deduplicated token snapshots into agent totals ──────
        for (_sess, agent_name), snap in token_snapshots.items():
            if agent_name not in agents:
                continue
            entry = agents[agent_name]
            pt = snap.get("prompt_tokens",     0)
            ct = snap.get("completion_tokens", 0)
            entry["total_tokens"]      += snap.get("total_tokens", 0)
            entry["prompt_tokens"]     += pt
            entry["completion_tokens"] += ct
            # ── Cost computation (platform-side via litellm) ───────────────
            model_name = snap.get("model", "")
            if model_name and pt > 0:
                try:
                    import litellm
                    lm = model_name if model_name.startswith("groq/") else f"groq/{model_name}"
                    cost_result = litellm.cost_per_token(
                        model=lm,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                    )
                    cost_total = sum(cost_result) if isinstance(cost_result, tuple) else float(cost_result)
                    entry["cost_usd"] += cost_total
                except Exception:
                    pass  # litellm not installed or model not in pricing table

        # ── Load declared agents from config_app_agents registry ──────────
        declared_agents: dict = {}
        try:
            q = db.query(AppAgent)
            if app_id:
                q = q.filter(AppAgent.app_id == app_id)
            for reg in q.all():
                declared_agents[reg.agent_name] = reg
        except Exception as _reg_exc:
            logger.warning("agent-activity: could not load agent registry: %s", _reg_exc)

        # ── Agent Activity is EXECUTION-DRIVEN (Stage 1) ──────────────────
        # We intentionally do NOT inject zero-traffic rows for declared-but-
        # unobserved agents. An agent appears here only when it has actually
        # produced evaluation events in the window. This prevents phantom
        # agents (e.g. card_researcher when the card-search flow never ran)
        # from polluting Agent Activity. The declared topology is still
        # visible via the registry (config_app_agents) and is used below only
        # as a classifier (is_declared) and as the topology-snapshot allowlist.

        # ── Load trust role weights for platform_accountability_score ────────
        # Weights gate how much each content role's COERCIVE verdicts degrade
        # the per-agent trust score (0.0 = no penalty, 1.0 = full penalty).
        # Defaults: user_prompt=0.0 (user-fault), all others=1.0 (agent-fault).
        try:
            _ta_row = (
                db.query(ServiceConfig)
                .filter(
                    ServiceConfig.service_name == "pap",
                    ServiceConfig.config_key   == "trust_accountability",
                )
                .first()
            )
            _ta_cfg = _ta_row.config_json if _ta_row else {}
            _trust_weights = {**_DEFAULT_TRUST_ROLE_WEIGHTS, **(_ta_cfg.get("trust_role_weights") or {})}
        except Exception:
            _trust_weights = dict(_DEFAULT_TRUST_ROLE_WEIGHTS)

        # ── Filter: only agents that are declared OR have tool/A2A evals ──
        # Pure routing nodes (router, transfer_router) only accumulate LLM
        # attribution evals (llm_response / user_prompt).  They are excluded
        # because they are coordination shims, not agents in the governance
        # sense.  Declared agents (is_declared_agent=True) are always shown.
        result = []
        for agent_name, entry in agents.items():
            reg         = declared_agents.get(agent_name)
            is_declared = bool(reg and reg.is_declared_agent)
            has_agent_role = bool(entry["roles"] & {"tool_call", "agent_to_agent"})

            # Exclude agents retired from the app's topology (removed from the
            # graph / declaration). They keep their history for audit, but should
            # not appear as live agents — matching the topology-snapshot allowlist.
            # Without this, a deleted agent (e.g. card_advisor after the card-search
            # flow was removed) lingers in Agent Activity via its historical events.
            if reg is not None and reg.status == "retired":
                continue

            if not is_declared and not has_agent_role:
                continue  # routing node — skip

            # ── Unsanctioned A2A detection ────────────────────────────────
            # An A2A eval for an agent that is NOT in the declared registry
            # indicates an out-of-band inter-agent handoff — a governance gap.
            unsanctioned_a2a = (
                "agent_to_agent" in entry["roles"]
                and not is_declared
            )

            avg_latency_ms = None
            if entry["_latency_count"] > 0:
                avg_latency_ms = round(entry["_latency_sum"] / entry["_latency_count"], 1)

            # ── Platform accountability score ───────────────────────────────
            # Starts at 1.0; each COERCIVE verdict on a content role reduces
            # the score proportionally by its configured weight.
            #   weighted_penalty = Σ violations_by_role[r] * weight[r]
            #   score = max(0.0, 1.0 - weighted_penalty / max(1, evaluations))
            # A zero-traffic agent stays at 1.0 (neutral — not yet observed).
            _evals = entry["evaluations"]
            _weighted_penalty = sum(
                entry["violations_by_role"].get(r, 0) * float(w)
                for r, w in _trust_weights.items()
            )
            _pa_score = round(
                max(0.0, 1.0 - _weighted_penalty / max(1, _evals)),
                3,
            )

            row = {
                "agent_name":                   entry["agent_name"],
                "evaluations":                  entry["evaluations"],
                "output_violations":            entry["output_violations"],
                "input_blocks":                 entry["input_blocks"],
                "violations":                   entry["violations"],   # total COERCIVE (backward compat)
                "warnings":                     entry["warnings"],
                "platform_accountability_score": _pa_score,
                "roles":                        sorted(entry["roles"]),
                "tools_called":                 sorted(entry["tools_called"]),
                "sessions":                     len(entry["sessions"]),
                "violation_dimensions":         entry["violation_dimensions"],
                "warning_dimensions":           entry["warning_dimensions"],
                "total_tokens":                 entry["total_tokens"],
                "prompt_tokens":                entry["prompt_tokens"],
                "completion_tokens":            entry["completion_tokens"],
                "cost_usd":                     round(entry["cost_usd"], 6),
                "is_declared_agent":            is_declared,
                "unsanctioned_a2a":             unsanctioned_a2a,
                "avg_latency_ms":               avg_latency_ms,
                # From the registry (empty lists when agent not yet registered)
                "declared_tools":               (reg.declared_tools or []) if reg else [],
                "a2a_targets":                  (reg.a2a_targets or []) if reg else [],
            }
            result.append(row)

        result.sort(key=lambda x: x["evaluations"], reverse=True)
        return {"agents": result, "computed_from": counted}

    @router.get("/v1/agent-topology/snapshot")
    def agent_topology_snapshot(
        app_id:     str     = Query("mybank", description="Application to snapshot"),
        window_min: int     = Query(15, ge=1, le=1440, description="Rolling window (minutes)"),
        db:         Session = Depends(get_db),
    ):
        """
        Roll the recent evaluation trail up into the 6-node MyBank agent topology
        for the dashboard's Agent Activity view.

        Reads the local audit trail directly (no cross-service hop), applies the
        agent alias table, span attribution, by-dimension violation routing and
        worst-verdict aggregation, and returns one AppSnapshot. All 6 nodes are
        always returned (zero counts for idle ones).
        """
        from datetime import datetime, timezone, timedelta

        # DYNAMIC roster: the nodes are discovered from the agents that actually
        # appear in the audit window — NOT a hard-coded list (which silently went
        # stale when the agent set changed, e.g. the Card-Denial reviewers never
        # showed). FOLD only normalises a few known aliases onto one node; any agent
        # name not in FOLD passes through as its own node, so new agents appear
        # automatically and old ones disappear when idle.
        # Alias canonicalisation — collapse the several names the SDK emits for one
        # logical agent (the council is registered as coordinator/policy_council/
        # payment_council; the router as router/transfer_router; etc.).
        # TODO(registry): move these aliases into a config_app_agents column so this
        # map can be retired — see docs/CHECKPOINT_AgentDiscovery_and_Topology.md.
        FOLD = {
            "policy_council":  "coordinator",
            "payment_council": "coordinator",
            "customer_profile": "profile",
            "transfer_router": "router",
            "account_agent":   "account",
            "card_profiler":   "card_advisor",
        }
        RANK = {"PERMISSIVE": 0, "NORMATIVE": 1, "COERCIVE": 2}
        PASS_STATUSES = {"PASS", "OK", ""}
        FETCH_LIMIT = int(os.getenv("TOPOLOGY_FETCH_LIMIT", "2000"))

        # ── Registry allowlist (Phase 3) ──────────────────────────────────────
        # The app's agent topology is the REGISTERED set (config_app_agents), not
        # whatever strings appear in traffic. Only agents that are registered and
        # NOT retired render. This structurally excludes:
        #   • fakes / labels never registered (banking_agent),
        #   • platform services (sentinel),
        #   • agents removed from the app (retired) or deprecated (preferred_wire_agent…).
        # No HIDE blocklist needed. If the app has never registered, fall back to
        # permissive discovery so the view still works.
        _reg_names = [
            r[0] for r in db.query(AppAgent.agent_name)
            .filter(AppAgent.app_id == app_id, AppAgent.status != "retired").all()
        ]
        VALID = {FOLD.get(n.strip().lower(), n.strip().lower()) for n in _reg_names if n}
        _use_registry = bool(VALID)

        # Defensive blocklist: governance-plane services must NEVER render inside a
        # business app's topology — even when the app's registry is empty (fallback
        # discovery) and a platform service leaked into the app's audit traffic
        # (e.g. Sentinel sync-detection writes a record under app_id=mybank). The
        # platform lens (app_id=trust-platform) is exempt, since those ARE its nodes.
        _PLATFORM_ONLY = {
            "sentinel", "gate", "structuring-detector",
            "collusion-detector", "conflict-detector", "policy-reviewer",
        }
        _is_platform_scope = (app_id == "trust-platform")

        def _alias(name):
            if not name:
                return None
            n = str(name).strip().lower()
            n = FOLD.get(n, n) or None
            if n is None:
                return None
            if not _is_platform_scope and n in _PLATFORM_ONLY:
                return None                      # platform service — never in an app topology
            if _use_registry and n not in VALID:
                return None                      # not a registered, live app agent
            return n

        def _parse_ts(raw):
            if not raw:
                return None
            txt = str(raw).strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(txt)
            except Exception:
                return None
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        def _dim_is_violation(dr):
            iv = dr.get("is_violation")
            if iv is True:
                return True
            if iv is None:
                return str(dr.get("status") or "").upper() not in PASS_STATUSES
            return False

        def _is_violation(rec):
            for dr in (rec.get("dimension_results") or []):
                if _dim_is_violation(dr):
                    return True
            if rec.get("violations"):
                return True
            return str(rec.get("verdict") or "").upper() == "COERCIVE"

        def _primary_dim(rec):
            for dr in (rec.get("dimension_results") or []):
                if _dim_is_violation(dr):
                    return str(dr.get("dimension") or "") or None
            return None

        def _route(dim, span_node):
            d = (dim or "").lower()
            if "structuring" in d:
                return "preferred_wire_agent"
            if d.startswith("sanctions") or d.startswith("aml") or d.startswith("concentration"):
                return "intl_wire_agent"
            if "pii" in d or "leakage" in d or "privacy" in d:
                return "account"
            return span_node

        def _category(dim):
            d = (dim or "").lower()
            if d.startswith("sanctions") or "aml" in d or "structuring" in d:
                return "AML"
            if "pii" in d or "privacy" in d:
                return "PII"
            if any(k in d for k in ("toxic", "harass", "hate", "offensive")):
                return "Safety"
            if "injection" in d or "jailbreak" in d:
                return "Prompt Injection"
            return (dim or "Policy").replace("_", " ").title()

        def _worse(a, b):
            ra = RANK.get((a or "").upper(), -1)
            rb = RANK.get((b or "").upper(), -1)
            return (b or "").upper() if rb > ra else a

        try:
            db.rollback()
        except Exception:
            pass
        from api.audit_db import load_audit_records
        try:
            records = load_audit_records(db, limit=FETCH_LIMIT, app_id=app_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"eval-trail read error: {exc}")

        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=window_min)
        acc   = {}            # node id -> {span, viol, verdict}  (discovered dynamically)
        edges = set()         # (source_node, acting_node) handoff pairs

        def _ensure(n):
            if n and n not in acc:
                acc[n] = {"span": 0, "viol": [], "verdict": None,
                          "outcome": None, "outcome_at": None}

        for rec in records:
            ts = _parse_ts(rec.get("timestamp"))
            if ts is not None and ts < cutoff:
                continue
            meta = rec.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            verdict = str(rec.get("verdict") or "").upper() or None
            role    = str(rec.get("role") or "")

            node = _alias(meta.get("agent_name"))
            if node is None and role == "agent_to_agent":
                node = _alias(meta.get("target_agent"))
            if node:
                _ensure(node)
                acc[node]["span"] += 1
                acc[node]["verdict"] = _worse(acc[node]["verdict"], verdict)
                # Emergent-risk OUTCOME tier (alert|watch|clear) carried by platform
                # detector emits — keep the latest one for this node (it is the agent's
                # determination, not the guardrail verdict on the emit content).
                _oc = str(meta.get("platform_outcome") or "").lower()
                if _oc in ("alert", "watch", "clear"):
                    if acc[node]["outcome_at"] is None or (ts is not None and ts >= acc[node]["outcome_at"]):
                        acc[node]["outcome"]    = _oc
                        acc[node]["outcome_at"] = ts

            # User-turn evaluations carry no agent_name — attribute them to the
            # synthetic User node so it reflects real operator activity (the human
            # DID interact with the app) instead of showing 0 evals / IDLE. We count
            # the evals + carry the overall verdict, but do NOT accumulate per-turn
            # dimension flags as "violations" on the User (the human isn't a governed
            # agent; input-layer flags shouldn't paint the User box as a warning).
            if role == "user_prompt":
                _ensure("user")
                acc["user"]["span"] += 1
                acc["user"]["verdict"] = _worse(acc["user"]["verdict"], verdict)

            # Handoff edge: who routed to whom (source_agent → the acting agent).
            if role == "agent_to_agent":
                src = _alias(meta.get("source_agent"))
                if src and node and src != node:
                    _ensure(src)
                    edges.add((src, node))
                    # Governance plane: a node's "activity" is what it DISPATCHES, not
                    # just what it receives. Sentinel is almost always the SOURCE (it
                    # fans work out to its detectors + policy-reviewer), so without this
                    # it would read 0 evals / IDLE and effectively vanish. Count the
                    # source's dispatch as its own activity — platform scope only, so
                    # business-app topologies (attributed to the acting agent) are
                    # unchanged.
                    if _is_platform_scope:
                        acc[src]["span"] += 1

            if _is_violation(rec) and node:
                dim = _primary_dim(rec)
                _ensure(node)
                acc[node]["viol"].append({
                    "type":    _category(dim),
                    "subtype": dim or "policy",
                    "at":      ts.isoformat() if ts else rec.get("timestamp"),
                    "snippet": rec.get("content_snippet"),
                })
                acc[node]["verdict"] = _worse(acc[node]["verdict"], verdict)

        # ── Sentinel's async detectors are always-present sub-agents ──────────
        # The three emergent detectors (structuring / collusion / conflict) are
        # event-driven: they only emit when a PE-flagged session fires, so on clean
        # traffic they would disappear from the topology entirely (unlike the gate /
        # Sentinel / policy-reviewer, which stay lit via continuous liveness emits).
        # They are REGISTERED sub-agents of Sentinel (config_app_agents), so always
        # render the three nested under the Sentinel hub — zero counts when idle.
        # Platform scope only; business-app topologies are untouched.
        if _is_platform_scope:
            _ensure("sentinel")
            # collusion-detector intentionally omitted — it overlaps the structuring
            # signal (collusion and structuring share the same aml signal), so it's
            # retired from the platform view to avoid a redundant node.
            for _det in ("structuring-detector", "conflict-detector"):
                _ensure(_det)
                edges.add(("sentinel", _det))

        # ── Root the graph at a synthetic User actor ──────────────────────────
        # The audit has no "agent" for the human turn, so the topology used to
        # start at the first agent, and any flow whose upstream agent is absent
        # from the window rendered ISOLATED (e.g. intl_wire_agent with no router
        # edge in view).  Add one "user" node and connect it to every agent that
        # has no incoming edge — giving the graph a single root and removing
        # orphans.  (The user node's own eval counts come from user_prompt rows above.)
        # The synthetic "User" root only makes sense for a BUSINESS app (the human
        # initiates the conversation). The governance plane has no user — its natural
        # root is the PE gate (the data-plane entry). Skip the User node there so the
        # platform lens doesn't show a stray, always-idle "User" tile.
        if acc and not _is_platform_scope:
            _USER = "user"
            _incoming = {t for (_s, t) in edges}
            _roots = [n for n in acc if n not in _incoming and n != _USER]
            if _roots:
                acc.setdefault(_USER, {"span": 0, "viol": [], "verdict": None})
                for _r in _roots:
                    edges.add((_USER, _r))

        agents = []
        total_spans = 0
        for n in sorted(acc.keys()):
            a = acc[n]
            total_spans += a["span"]
            viols = sorted(a["viol"], key=lambda v: v.get("at") or "", reverse=True)
            agents.append({
                "id":               n,
                "violation_count":  len(viols),
                "violations":       viols[:10],
                "last_violation_at": (viols[0]["at"] if viols else None),
                "span_count":       a["span"],
                "eval_count":       a["span"],     # all evaluations (for the "show all" toggle)
                "live_verdict":     a["verdict"],
                "outcome":          a.get("outcome"),   # emergent-risk tier: alert|watch|clear
            })

        return {
            "app_id":           app_id,
            "as_of":            now.isoformat(),
            "window_min":       window_min,
            "total_span_count": total_spans,
            "agents":           agents,
            "edges":            [list(e) for e in sorted(edges)],
        }

    # ================================================================== #
    #  CORPORATE POLICY — signal list editor                             #
    #  GET  /v1/corporate-policy/signals  → proxy to GS                 #
    #  PUT  /v1/corporate-policy/signals  → proxy to GS, audit log      #
    # ================================================================== #

    @router.get("/v1/corporate-policy/signals")
    def get_corporate_policy_signals():
        """Return current corporate_policy signal list and enforce_mode from GS."""
        try:
            r = httpx.get(f"{GUARDRAIL_URL}/internal/corporate-policy/signals", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.put("/v1/corporate-policy/signals")
    def put_corporate_policy_signals(
        body:  dict,
        actor: str = Depends(_get_actor),
        db:    Session = Depends(get_db),
    ):
        """
        Persist an updated corporate_policy signal list and enforce_mode.
        Proxies to GS which writes policy_signals.yaml and hot-reloads the classifier.
        Writes a PAP audit entry for governance traceability.
        """
        try:
            r = httpx.put(
                f"{GUARDRAIL_URL}/internal/corporate-policy/signals",
                json=body, timeout=5.0,
            )
            if r.status_code == 200:
                db.add(PapAuditLog(
                    entity_type="corporate_policy",
                    entity_id="corporate_policy",
                    action="UPDATE",
                    actor=actor,
                    new_values=json.dumps({
                        "keywords":     body.get("keywords", []),
                        "enforce_mode": body.get("enforce_mode", "warn"),
                        "count":        len(body.get("keywords", [])),
                    }),
                ))
                db.commit()
                logger.info(
                    f"[PAP] corporate-policy signals updated by {actor}: "
                    f"{len(body.get('keywords', []))} keywords, "
                    f"enforce_mode={body.get('enforce_mode', 'warn')}"
                )
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    # ================================================================== #
    #  SESSION POLICY  (proxy to Policy Engine)                          #
    #  GET  /v1/session-policy   → current live session trust config     #
    #  PUT  /v1/session-policy   → update + hot-reload, audit log entry  #
    # ================================================================== #

    @router.get("/v1/session-policy")
    def get_session_policy():
        """Return the current session tracking policy from the Policy Engine."""
        try:
            r = httpx.get(f"{POLICY_ENGINE_URL}/v1/session-policy", timeout=5.0)
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    @router.put("/v1/session-policy")
    def put_session_policy(
        body:  dict,
        actor: str = Depends(_get_actor),
        db:    Session = Depends(get_db),
    ):
        """
        Update session tracking policy via the Policy Engine and record an audit entry.
        Accepts the same structure as GET /v1/session-policy (partial updates supported).
        """
        try:
            r = httpx.put(
                f"{POLICY_ENGINE_URL}/v1/session-policy",
                json=body,
                timeout=5.0,
            )
            if r.status_code == 200:
                db.add(PapAuditLog(
                    entity_type="session_policy",
                    entity_id="session_policy",
                    action="UPDATE",
                    actor=actor,
                    new_values=json.dumps(body),
                ))
                db.commit()
                logger.info(f"[PAP] session-policy updated by {actor}")
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    # ================================================================== #
    #  TRUST ACCOUNTABILITY — role-specific trust impact weights         #
    #                                                                    #
    #  These multipliers gate whether a COERCIVE verdict on a given     #
    #  content role degrades the app's platform trust score.            #
    #                                                                    #
    #  Default semantics:                                                #
    #    user_prompt    = 0.0 → user-fault (attacker), no app penalty   #
    #    agent_to_agent = 1.0 → agent routing was compromised           #
    #    llm_response   = 1.0 → agent produced violating output         #
    #    tool_call      = 1.0 → agent attempted a blocked action        #
    #                                                                    #
    #  Stored in ServiceConfig('pap', 'trust_accountability').          #
    #  PE reads these at hot-reload and applies them when updating the  #
    #  rolling PeAppTrustScore on each evaluation.                      #
    # ================================================================== #

    _DEFAULT_TRUST_ROLE_WEIGHTS: dict = {
        "user_prompt":    0.0,
        "agent_to_agent": 1.0,
        "llm_response":   1.0,
        "tool_call":      1.0,
    }

    @router.get("/v1/trust-accountability")
    def get_trust_accountability(db: Session = Depends(get_db)):
        """
        Return the current trust accountability role-weight configuration.

        The weights gate whether COERCIVE verdicts for each content role
        degrade the app's platform trust score.
        Returns PAP-stored values; falls back to defaults if not yet persisted.
        """
        row = (
            db.query(ServiceConfig)
            .filter(
                ServiceConfig.service_name == "pap",
                ServiceConfig.config_key   == "trust_accountability",
            )
            .first()
        )
        cfg = row.config_json if row else {}
        weights = {**_DEFAULT_TRUST_ROLE_WEIGHTS, **(cfg.get("trust_role_weights") or {})}
        return {
            "trust_role_weights": weights,
            "semantics": {
                k: ("platform fault — degrades app trust" if v > 0 else "user fault — no app penalty")
                for k, v in weights.items()
            },
        }

    @router.put("/v1/trust-accountability")
    def put_trust_accountability(
        body:  dict,
        actor: str    = Depends(_get_actor),
        db:    Session = Depends(get_db),
    ):
        """
        Update trust accountability role weights.

        Body: {"trust_role_weights": {"user_prompt": 0.0, "llm_response": 1.0, ...}}
        Partial updates are supported — only provided roles are changed.
        """
        new_weights_in = body.get("trust_role_weights") or {}
        # Validate: all values must be 0.0–1.0 floats
        valid_roles = set(_DEFAULT_TRUST_ROLE_WEIGHTS)
        for role_key, weight in new_weights_in.items():
            if role_key not in valid_roles:
                raise HTTPException(status_code=422, detail=f"Unknown role '{role_key}'")
            if not isinstance(weight, (int, float)) or not (0.0 <= float(weight) <= 1.0):
                raise HTTPException(status_code=422, detail=f"Weight for '{role_key}' must be 0.0–1.0")

        row = (
            db.query(ServiceConfig)
            .filter(
                ServiceConfig.service_name == "pap",
                ServiceConfig.config_key   == "trust_accountability",
            )
            .first()
        )
        old_cfg = row.config_json if row else {}
        old_weights = {**_DEFAULT_TRUST_ROLE_WEIGHTS, **(old_cfg.get("trust_role_weights") or {})}
        new_weights  = {**old_weights, **{k: float(v) for k, v in new_weights_in.items()}}
        new_cfg = {"trust_role_weights": new_weights}

        if row:
            row.config_json = new_cfg
        else:
            row = ServiceConfig(
                service_name="pap",
                config_key="trust_accountability",
                config_json=new_cfg,
            )
            db.add(row)

        db.add(PapAuditLog(
            entity_type="trust_accountability",
            entity_id="trust_accountability",
            action="UPDATE",
            actor=actor,
            old_values=json.dumps({"trust_role_weights": old_weights}),
            new_values=json.dumps(new_cfg),
        ))
        db.commit()
        return {"trust_role_weights": new_weights, "updated_by": actor}

    # ================================================================== #
    #  SESSIONS — for Trust Agents                                        #
    # ================================================================== #

    @router.get("/v1/sessions/summaries")
    def session_summaries(
        window_hours: int          = Query(1, ge=1, le=168, description="Look-back window in hours"),
        limit:        int          = Query(100, ge=1, le=1000),
        app_id:       Optional[str] = Query(None, description="Filter to a specific application"),
    ):
        """
        Recent pe_session_summaries for the Trust Tracker agent.
        Reads directly from the shared PAP SQLite — no PE network call needed.
        Returns: coercive_count, final_trust_score, turn_count, end_reason per session.
        """
        import sqlite3, time
        # Derive the SQLite path from PAP_DATABASE_URL if possible, else use the
        # standard PAP config directory location.
        _pap_db_url = os.getenv("PAP_DATABASE_URL", "")
        if _pap_db_url.startswith("sqlite:///"):
            # Relative path from CWD (PAP is always started from pap/)
            _rel = _pap_db_url[len("sqlite:///"):]
            _db_candidate = Path(_rel)
            if not _db_candidate.is_absolute():
                _db_candidate = Path(__file__).parent.parent / _rel
        else:
            _db_candidate = Path(__file__).parent.parent / "config" / "pap.db"
        db_path = os.getenv("AUDIT_SQLITE_PATH", str(_db_candidate))
        cutoff_ts = time.time() - (window_hours * 3600)
        try:
            con = sqlite3.connect(db_path, timeout=5)
            con.row_factory = sqlite3.Row
            _app_filter = " AND app_id = ?" if app_id else ""
            _params = [cutoff_ts] + ([app_id] if app_id else []) + [limit]
            rows = con.execute(
                f"""SELECT session_id, app_id, end_reason,
                          turn_count, coercive_count, final_trust_score,
                          started_at_ts, ended_at_ts
                   FROM pe_session_summaries
                   WHERE ended_at_ts >= ?{_app_filter}
                   ORDER BY ended_at_ts DESC
                   LIMIT ?""",
                _params,
            ).fetchall()
            con.close()
            return {
                "sessions":      [dict(r) for r in rows],
                "count":         len(rows),
                "window_hours":  window_hours,
            }
        except Exception as exc:
            logger.warning(f"[PAP /v1/sessions/summaries] {exc}")
            return {"sessions": [], "count": 0, "window_hours": window_hours}

    # ================================================================== #
    #  HEALTH                                                             #
    # ================================================================== #

    @router.get("/v1/health")
    def health(db: Session = Depends(get_db)):
        """
        FAIL-CLOSED readiness self-test. PAP reports healthy (HTTP 200) ONLY when
        the platform can actually govern:
          1. the database is reachable, AND
          2. there is >= 1 ACTIVE policy loaded, AND
          3. the Guardrail Service (the scorer) responds.
        If any check fails, returns HTTP 503 with the failing reason(s). This makes
        a misconfigured deploy report UNHEALTHY instead of silently-green — the whole
        point: no valid policies or no Guardrail => not healthy.
        """
        checks: dict = {}
        reasons: list = []

        # 1. DB + active policies
        try:
            policy_count = db.query(Policy).filter(Policy.active == True).count()  # noqa: E712
            checks["database"] = "ok"
            checks["active_policies"] = policy_count
            if policy_count == 0:
                reasons.append("no active policies loaded (seeding did not run / empty DB)")
        except Exception as e:
            policy_count = 0
            checks["database"] = f"error: {e}"
            checks["active_policies"] = 0
            reasons.append(f"database unreachable: {e}")

        # 2. Guardrail Service reachable (the scorer)
        try:
            import httpx as _httpx
            r = _httpx.get(f"{GUARDRAIL_URL}/v1/health", timeout=3.0)
            if r.status_code == 200:
                checks["guardrail"] = "ok"
                checks["guardrail_mock_mode"] = r.json().get("mock_mode")
            else:
                checks["guardrail"] = f"http {r.status_code}"
                reasons.append(f"guardrail returned HTTP {r.status_code}")
        except Exception as e:
            checks["guardrail"] = f"unreachable: {e}"
            reasons.append(f"guardrail unreachable at {GUARDRAIL_URL}: {e}")

        ok = not reasons
        body = {
            "status":       "healthy" if ok else "unhealthy",
            "service":      "pap",
            "version":      "0.2.0",
            "checks":       checks,
            "policy_count": policy_count,
            "reasons":      reasons,
        }
        # Fail-closed: 503 when not ready, so orchestrators/load-balancers don't
        # route traffic to a PAP that can't actually govern.
        return JSONResponse(status_code=200 if ok else 503, content=body)

    @router.get("/v1/diag/config")
    def diag_config(db: Session = Depends(get_db)):
        """
        Consolidated configuration diagnostic — answers, in one call:
          • Did the policy config load?            -> policies.active / .total
          • Which provider scores each dimension?  -> dimension_provider_map
          • What's in the provider registry?       -> providers[]
          • Did anything silently go wrong?        -> warnings[]

        Use this (or the [PAP CONFIG] startup log banner) instead of inferring
        from a green/empty dashboard. Note: the map shows the *configured* provider
        per dimension; the *runtime* scorer also depends on the Guardrail Service's
        MOCK_MODE -- confirm that at GET <guardrail>/v1/health ("mock_mode").
        """
        out: dict = {"service": "pap", "ok": True, "warnings": []}
        try:
            pol_total  = db.query(Policy).count()
            pol_active = db.query(Policy).filter(Policy.active == True).count()  # noqa: E712

            dims        = db.query(EvaluationDimension).all()
            dim_active  = [d for d in dims if d.status == "active"]
            provider_map = {d.dimension_id: d.provider for d in dim_active}

            provs       = db.query(Provider).all()
            prov_rows   = [{
                "id":     p.provider_id,
                "type":   getattr(p, "provider_type", "?"),
                "active": bool(getattr(p, "active", True)),
            } for p in provs]
            active_prov_ids = {p["id"] for p in prov_rows if p["active"]}

            out["policies"]              = {"total": pol_total, "active": pol_active}
            out["dimensions"]            = {"total": len(dims), "active": len(dim_active)}
            out["providers"]             = prov_rows
            out["dimension_provider_map"] = provider_map

            # ── The checks that catch the silent "everything green" failures ──
            if pol_active == 0:
                out["warnings"].append(
                    "0 ACTIVE policies -- every evaluation will return PERMISSIVE. "
                    "Seeding likely did not run or the DB is empty."
                )
            if not active_prov_ids:
                out["warnings"].append("0 ACTIVE providers registered.")
            unbacked = sorted({prov for prov in provider_map.values()
                               if prov not in active_prov_ids})
            if unbacked:
                out["warnings"].append(
                    "Active dimensions reference provider(s) not active in the registry: "
                    f"{unbacked}. In MOCK_MODE the Guardrail repoints these to mock_provider; "
                    "if MOCK_MODE is false those dimensions fail-open PASS."
                )
            out["ok"] = (pol_active > 0)
        except Exception as e:
            out["ok"] = False
            out["error"] = str(e)
        return out



    # ================================================================== #
    #  PRIVACY PROXY — PII scan test endpoint                            #
    # ================================================================== #

    @router.post("/v1/proxy/pii-scan")
    def pii_scan(req: PiiScanRequest):
        """
        Test text through the privacy proxy PII detection patterns.
        Returns the anonymised text and list of detected PII labels.
        Identical patterns to rai_privacy_proxy.PrivacyProxy Stage 1.
        """
        import re as _re

        _PATTERNS = [
            # Financial — account number (6-17 digits after keyword).
            # Pattern: keyword + optional(space+"number/num/no") + space + digits
            # Separator space is OUTSIDE the optional group — avoids ambiguous
            # backtracking when "number" is absent (e.g. "account 1234567890").
            ("account_num",  r"\g<1>[ACCOUNT]",
             _re.compile(r"(?i)\b((?:account|acct?)(?:\s+(?:number|num|no\.?))?\s+)(\d{6,17})\b")),
            # Financial — routing / ABA / transit (exactly 9 digits after keyword).
            ("routing_num",  r"\g<1>[ROUTING]",
             _re.compile(r"(?i)\b((?:routing|aba|transit)(?:\s+(?:number|num|no\.?))?\s+)(\d{9})\b")),
            # Credit card (16 digits, 4-4-4-4)
            ("credit_card",  "[CARD]",
             _re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")),
            # SSN  NNN-NN-NNNN
            ("ssn",          "[SSN]",
             _re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
            # Email
            ("email",        "[EMAIL]",
             _re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
            # Phone (10-digit)
            ("phone",        "[PHONE]",
             _re.compile(r"\b(\+\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")),
            # API keys / secrets
            ("api_key",      "[API_KEY]",
             _re.compile(r"(?i)(sk-|api[_-]?key\s*[=:]\s*|bearer\s+)[A-Za-z0-9_\-\.]{20,}")),
        ]

        text = req.text or ""
        anonymized = text
        detections: list = []
        for label, token, pattern in _PATTERNS:
            if pattern.search(anonymized):
                anonymized = pattern.sub(token, anonymized)
                detections.append(label)

        return {
            "original":   text,
            "anonymized": anonymized,
            "detections": detections,
            "changed":    anonymized != text,
        }

    # ================================================================== #
    #  HITL — Human-In-The-Loop review queue                             #
    # ================================================================== #

    @router.post("/internal/hitl")
    async def hitl_intake(request: Request):
        """
        Receive a HITL event from a governed application (e.g. myBank).

        Called fire-and-forget by GovernedGraph when a NORMATIVE A2A verdict
        meets the configured threshold.  Stores the item in _hitl_items so
        the Human Review tab can display it immediately.
        """
        try:
            body = await request.json()
        except Exception:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        session_id = body.get("session_id") or f"unknown-{datetime.utcnow().timestamp()}"
        _hitl_items[session_id] = {
            "session_id":       session_id,
            "app_id":           body.get("app_id", "unknown"),
            "reason":           body.get("reason", "governance_review_required"),
            "verdict":          body.get("verdict", "NORMATIVE"),
            "violations":       body.get("violations", []),
            "agent":            body.get("agent", ""),
            "tool":             body.get("tool", ""),
            "agent_display":    body.get("agent_display", ""),
            "tool_display":     body.get("tool_display", ""),
            "normative_count":  body.get("normative_count", 1),
            "reviewer_advisory": body.get("reviewer_advisory", ""),
            "callback_url":     body.get("callback_url", ""),
            "received_at":      datetime.utcnow().isoformat() + "Z",
            "status":           "pending",
            # Card-recommendation fields (Story 4 — populated when hitl_type == "card_recommendation")
            "hitl_type":        body.get("hitl_type", ""),
            "card_options":     body.get("card_options", []),
            "recommended_card": body.get("recommended_card", ""),
            "conflict_detected": body.get("conflict_detected", False),
            # Card Authorization Review council fields (Story 5 — when hitl_type == "council")
            "council_members":        body.get("council_members", []),
            "recommended_resolution": body.get("recommended_resolution", ""),
            "precedence":             body.get("precedence", []),
            "card_surge":             body.get("card_surge", {}),
            "new_evidence":           body.get("new_evidence", ""),
            "deliberation_rounds":    body.get("deliberation_rounds", 0),
            "vote_tally":             body.get("vote_tally", {}),
            "conflict_summary":       body.get("conflict_summary", ""),
            "stage":                  body.get("stage", 0),
            "verification_question":  body.get("verification_question", ""),
            "initial_decision":       body.get("initial_decision", ""),
        }
        logger.info("[PAP] HITL item received — session=%s app=%s agent=%s",
                    session_id, body.get("app_id"), body.get("agent"))

        # Write initiation event to PE audit trail (platform-side, fire-and-forget)
        asyncio.create_task(_write_hitl_audit_to_pe(
            session_id = session_id,
            app_id     = body.get("app_id", "unknown"),
            event_type = "hitl_initiated",
            decision   = "pending",
            reviewer   = "",
            agent      = body.get("agent", ""),
            tool       = body.get("tool", ""),
        ))

        return {"accepted": True, "session_id": session_id}

    @router.get("/v1/hitl")
    def list_hitl_items():
        """
        Return all HITL items (pending and resolved) ordered newest-first.

        The Human Review tab polls this endpoint every 5 seconds.
        """
        items = sorted(
            _hitl_items.values(),
            key=lambda x: x.get("received_at", ""),
            reverse=True,
        )
        return {
            "items":   items,
            "count":   len(items),
            "pending": sum(1 for i in items if i.get("status") == "pending"),
        }

    @router.get("/v1/hitl/{session_id}")
    def get_hitl_item(session_id: str):
        """
        Pull the authoritative decision for one HITL session. This is the
        reliability backstop: the application (e.g. MyBank) polls this so it
        receives the reviewer's decision even when PAP's outbound callback to the
        app failed or the app's callback URL is wrong. PAP's store is the source
        of truth — the decision recorded here is final.
        """
        item = _hitl_items.get(session_id)
        if not item:
            return {"found": False, "session_id": session_id}
        return {
            "found":       True,
            "session_id":  session_id,
            "status":      item.get("status"),       # "pending" | "resolved"
            "decision":    item.get("decision"),     # "approved" | "rejected" | "request_verification" | card_id
            "reviewer":    item.get("reviewer"),
            "resolved_at": item.get("resolved_at"),
        }

    @router.post("/v1/hitl/{session_id}/resolve")
    async def resolve_hitl_item(session_id: str, request: Request):
        """
        Approve or reject a HITL item and forward the decision to the
        application's callback URL.

        Body: {"decision": "approved" | "rejected", "reviewer": "<name>"}
        """
        from fastapi import HTTPException
        item = _hitl_items.get(session_id)
        if not item:
            raise HTTPException(status_code=404, detail="HITL item not found")
        if item.get("status") != "pending":
            raise HTTPException(status_code=409, detail="HITL item already resolved")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        decision = body.get("decision", "rejected")
        reviewer = body.get("reviewer", "pap-reviewer")

        is_card_hitl = item.get("hitl_type") == "card_recommendation"
        is_council   = item.get("hitl_type") == "council"
        valid_card_ids = {opt.get("card_id") for opt in item.get("card_options", [])}
        # Council stage-1 adds a third action: request_verification (ask the customer).
        _allowed = ("approved", "rejected") + (("request_verification",) if is_council else ())
        if not is_card_hitl and decision not in _allowed:
            raise HTTPException(status_code=400, detail=f"decision must be one of {_allowed}")
        if is_card_hitl and decision not in ("approved", "rejected") and decision not in valid_card_ids:
            raise HTTPException(status_code=400, detail=f"decision must be 'approved', 'rejected', or a valid card_id")

        callback_url = item.get("callback_url", "")

        # ── PAP is the SOURCE OF TRUTH ────────────────────────────────────────
        # Record the decision in PAP's durable store FIRST, before any network
        # call. The reviewer's action therefore succeeds regardless of whether the
        # application is reachable right now. The app (e.g. MyBank) receives the
        # decision either via the best-effort push below OR by polling
        # GET /v1/hitl/{session_id}. This removes the fragile dependency on PAP
        # being able to reach the app's inbound callback URL.
        _hitl_items[session_id] = {
            **item,
            "status":      "resolved",
            "decision":    decision,
            "reviewer":    reviewer,
            "resolved_at": datetime.utcnow().isoformat() + "Z",
        }
        logger.info("[PAP] HITL resolved (recorded) — session=%s decision=%s reviewer=%s",
                    session_id, decision, reviewer)

        # ── Best-effort push (fast path) — retries, NEVER blocks the reviewer ──
        # If delivery fails, the app's poll backstop picks the decision up. The
        # app's /api/hitl/resolve is idempotent, so a later duplicate is harmless.
        delivered = False
        if callback_url:
            for _attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r = await client.post(callback_url, json={
                            "session_id": session_id,
                            "decision":   decision,
                            "reviewer":   reviewer,
                        })
                    if r.is_success:
                        delivered = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.4 * (_attempt + 1))
            if not delivered:
                logger.warning(
                    "[PAP] HITL push to app failed after retries (callback=%s) — "
                    "decision is recorded; app will pull it via GET /v1/hitl/%s",
                    callback_url, session_id,
                )
        else:
            logger.warning("[PAP] HITL item %s has no callback_url — app must pull the decision.", session_id)

        # Write resolution event to PE audit trail (platform-side, fire-and-forget)
        asyncio.create_task(_write_hitl_audit_to_pe(
            session_id = session_id,
            app_id     = item.get("app_id", "unknown"),
            event_type = "hitl_resolved",
            decision   = decision,
            reviewer   = reviewer,
            agent      = item.get("agent", ""),
            tool       = item.get("tool", ""),
        ))

        return {"resolved": True, "session_id": session_id, "decision": decision, "delivered": delivered}

    # ------------------------------------------------------------------ #
    #  DELETE /v1/hitl/{session_id}   — dismiss a single item             #
    #  POST   /v1/hitl/clear-resolved — drop all resolved items           #
    # ------------------------------------------------------------------ #
    @router.delete("/v1/hitl/{session_id}")
    def dismiss_hitl_item(session_id: str):
        """Remove a HITL item from the queue (dismiss — does NOT notify the app)."""
        existed = _hitl_items.pop(session_id, None) is not None
        return {"dismissed": existed, "session_id": session_id}

    @router.post("/v1/hitl/clear-resolved")
    def clear_resolved_hitl_items():
        """Drop every already-resolved item from the queue, leaving pending ones."""
        resolved = [sid for sid, it in _hitl_items.items() if it.get("status") != "pending"]
        for sid in resolved:
            _hitl_items.pop(sid, None)
        return {"cleared": len(resolved)}

    # ⚠️ TEMPORARY / DEMO-ONLY — remove before any non-demo release.
    @router.post("/v1/hitl/clear-all")
    def clear_all_hitl_items():
        """⚠️ TEMPORARY (demo convenience) — drop EVERY HITL item, pending AND resolved.

        Lets a presenter reset the Human Review queue between demo runs in one click.
        It wipes pending items WITHOUT routing them through the proper resolve/decision
        flow and WITHOUT notifying the originating app, so it is NOT production-safe.
        Remove this endpoint together with the "Clear all" button in dashboard.html
        (clearAllHITL) once the demo phase is over.
        """
        n = len(_hitl_items)
        _hitl_items.clear()
        return {"cleared": n}

    # ── Agent Cards / Agent Registry ──────────────────────────────────────────
    # Per-agent governance metadata (config_agent_cards, operator-seeded) MERGED
    # with the live topology row (config_app_agents: tools, a2a_targets, status)
    # and runtime metrics + trust score computed from the evaluation audit.
    #
    # Trust score — ASYMMETRIC PER OUTCOME ("hard to gain, easy to lose"):
    #   start at 80, then per evaluation outcome apply a small fixed tick.
    #   A clean PERMISSIVE nudges up a little; a NORMATIVE (human-review) dips it;
    #   a COERCIVE (blocked) costs more; a Sentinel-confirmed authority breach
    #   (governance evasion / circuit-breaker freeze) is a heavy penalty on top.
    _TRUST_BASELINE   = 80.0
    _TRUST_W_PERM     = +0.2     # tiny gain — hard to climb
    _TRUST_W_NORM     = -1.0     # small loss
    _TRUST_W_COER     = -3.0     # larger loss — easy to lose
    _TRUST_W_BREACH   = -20.0    # severe: confirmed authority/governance breach

    def _trust_score(perm, norm, coer, breaches):
        s = (_TRUST_BASELINE + perm * _TRUST_W_PERM + norm * _TRUST_W_NORM
             + coer * _TRUST_W_COER + breaches * _TRUST_W_BREACH)
        return int(round(max(0.0, min(100.0, s))))   # whole-number score (no decimals)

    def _trust_band(score):
        return "high" if score >= 75 else ("medium" if score >= 50 else "low")

    def _agent_audit_stats(db, app_id: str) -> dict:
        """One pass over the app's evaluation audit, attributed to each agent via
        metadata_json.agent_name. Returns agent_name -> running counts. Authority
        breaches are Sentinel-confirmed governance-evasion / circuit-breaker freezes
        detected in the violation strings."""
        from sqlalchemy import text as _text
        out = {}
        rows = db.execute(
            _text("SELECT verdict, violations, metadata_json, timestamp, "
                  "input_tokens, output_tokens, model_id "
                  "FROM audit_evaluation_audit WHERE app_id = :app "
                  "AND metadata_json IS NOT NULL"),
            {"app": app_id},
        ).fetchall()
        for verdict, violations, meta, ts, in_tok, out_tok, model_id in rows:
            try:
                md = json.loads(meta) if meta else None
            except Exception:
                md = None
            if not isinstance(md, dict):
                continue
            agent = md.get("agent_name")
            if not agent:
                continue
            s = out.setdefault(agent, {
                "evaluations": 0, "permissive": 0, "normative": 0,
                "coercive": 0, "authority_breaches": 0, "last_active": None,
                "tokens_in": 0, "tokens_out": 0, "model_id": None,
            })
            s["evaluations"] += 1
            v = (verdict or "").upper()
            if v == "PERMISSIVE":
                s["permissive"] += 1
            elif v == "NORMATIVE":
                s["normative"] += 1
            elif v == "COERCIVE":
                s["coercive"] += 1
            # authority breach: Sentinel-confirmed evasion / circuit-breaker freeze
            vraw = (violations or "")
            if isinstance(vraw, str) and ("evade governance" in vraw or "circuit_breaker: session frozen" in vraw):
                s["authority_breaches"] += 1
            if ts and (s["last_active"] is None or str(ts) > s["last_active"]):
                s["last_active"] = str(ts)
            s["tokens_in"]  += int(in_tok or 0)
            s["tokens_out"] += int(out_tok or 0)
            if model_id and not s["model_id"]:
                s["model_id"] = model_id
        # Per-agent token cost — reuse the COGS pricing table (same source as the
        # Token-Cost tile), so the scorecard's $ figure is consistent platform-wide.
        try:
            from api.cogs import _get_pricing_map, _match_price
            pricing = _get_pricing_map(db)
        except Exception:
            pricing = {}
        for s in out.values():
            price = _match_price(s.get("model_id"), pricing) if pricing else None
            if price and (s["tokens_in"] or s["tokens_out"]):
                cin, cout = price
                s["cost_usd"] = round(s["tokens_in"] * cin / 1_000_000
                                      + s["tokens_out"] * cout / 1_000_000, 6)
            else:
                s["cost_usd"] = None
        return out

    def _jload(v):
        if isinstance(v, (list, dict)):
            return v
        try:
            return json.loads(v or "[]")
        except Exception:
            return []

    def _build_card(row, topo: dict, stats: dict) -> dict:
        """Merge a (possibly absent) seeded card + live topology row + audit stats.
        `row` may be a DB mapping OR a synthesized dict (for a registered agent that
        has no operator-authored card yet) — use a tolerant getter for both."""
        g = (lambda k: row.get(k)) if isinstance(row, dict) else (lambda k: row[k] if k in row else None)
        agent_id = g("agent_id")
        t = topo.get(agent_id, {})
        s = stats.get(agent_id, {})
        perm = s.get("permissive", 0); norm = s.get("normative", 0)
        coer = s.get("coercive", 0); breaches = s.get("authority_breaches", 0)
        score = _trust_score(perm, norm, coer, breaches)
        return {
            "app_id":             g("app_id"),
            "agent_id":           agent_id,
            "name":               g("name"),
            "type":               g("agent_type"),
            "capability":         g("capability"),
            "purpose":            g("purpose"),
            "framework":          g("framework"),
            "model_id":           g("model_id"),
            "domain":             g("domain"),
            "data_access":        _jload(g("data_access")),
            "escalation":         g("escalation"),
            "allowed_actions":    _jload(g("allowed_actions")),
            "prohibited_actions": _jload(g("prohibited_actions")),
            "max_authority":      g("max_authority"),
            # Governance config flags (SQLite stores bool as int; NULL on pre-migration rows)
            "kill_switch":        bool(g("kill_switch") or 0),
            "hitl_mode":          (g("hitl_mode") or "none"),
            "hitl_escalation":    (g("hitl_mode") or "none") != "none",  # derived from hitl_mode
            "seeded":             not isinstance(row, dict),  # False = no operator card yet
            # live topology (config_app_agents): tools + who it actually INVOKES.
            # `invokes` prefers the OBSERVED handoffs from the audit trail (real
            # source_agent → target_agent edges). a2a_targets is only the app-wide
            # reachable roster — identical on every declared agent — so it is the
            # fallback for an agent that has not yet made an A2A call, never the
            # primary. invokes_source tells the operator which one they're seeing.
            "tools":              t.get("declared_tools") or [],
            "invokes":            (t.get("observed_invokes")
                                   or [x for x in (t.get("a2a_targets") or []) if x != agent_id]),
            "invokes_source":     ("observed" if t.get("observed_invokes")
                                   else ("declared" if t.get("a2a_targets") else "none")),
            "a2a_targets":        t.get("a2a_targets") or [],
            "declared":           bool(t.get("is_declared_agent")),
            "status":             t.get("status") or "unknown",
            # runtime metrics
            "metrics": {
                "evaluations":        s.get("evaluations", 0),
                "permissive":         perm,
                "normative":          norm,
                "coercive":           coer,
                "violations":         norm + coer,
                "authority_breaches": breaches,
                "trust_score":        score,
                "trust_band":         _trust_band(score),
                "last_active":        s.get("last_active"),
                "cost_usd":           s.get("cost_usd"),
                "tokens":             (s.get("tokens_in", 0) + s.get("tokens_out", 0)),
            },
        }

    def _topo_map(db, app_id: str) -> dict:
        observed = _observed_invokes(db, app_id)
        out = {}
        for a in db.query(AppAgent).filter(AppAgent.app_id == app_id).all():
            out[a.agent_name] = {
                "declared_tools":    a.declared_tools or [],
                "a2a_targets":       a.a2a_targets or [],
                "is_declared_agent": a.is_declared_agent,
                "status":            a.status,
                "observed_invokes":  observed.get(a.agent_name, []),
            }
        return out

    def _observed_invokes(db, app_id: str) -> dict:
        """
        Real per-agent A2A callees, read from the evaluation audit trail.

        config_app_agents.a2a_targets is an APP-WIDE roster: the SDK's declaration
        payload carries {tool_name: target_agent} with no caller attribution, so
        /internal/register-agents stamps the same "every reachable agent" list onto
        every declared agent. Presenting that as a per-agent "Invokes" list is
        wrong — it renders an identical complete graph on every card (e.g.
        Cross-Border Logistics appearing to invoke the whole review council).

        agent_to_agent audit records DO carry source_agent → target_agent, so the
        observed handoffs are authoritative. Returns {source: sorted[targets]};
        callers fall back to the declared roster for agents with no observed A2A.
        """
        from api.audit_db import load_audit_records
        try:
            db.rollback()
        except Exception:
            pass
        try:
            records = load_audit_records(db, limit=5000, app_id=app_id)
        except Exception:
            return {}

        out: dict = {}
        for rec in records:
            if str(rec.get("role") or "") != "agent_to_agent":
                continue
            meta = rec.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    continue
            src = meta.get("source_agent")
            tgt = meta.get("target_agent") or meta.get("agent_name")
            if src and tgt and src != tgt:
                out.setdefault(str(src), set()).add(str(tgt))
        return {k: sorted(v) for k, v in out.items()}

    @router.get("/v1/agent-cards/apps")
    def list_agent_card_apps(db: Session = Depends(get_db)):
        """Distinct app_ids that have agent cards — drives the registry app selector."""
        from sqlalchemy import text as _text
        rows = db.execute(
            _text("SELECT app_id, COUNT(*) n FROM config_agent_cards GROUP BY app_id ORDER BY app_id")
        ).fetchall()
        return {"apps": [{"app_id": r[0], "count": r[1]} for r in rows]}

    @router.get("/v1/agent-cards")
    def list_agent_cards(
        app_id: str     = Query("mybank", description="Application to list agent cards for"),
        db:     Session = Depends(get_db),
    ):
        """Grid of agent cards — EVERY registered agent (#2), not just seeded ones.
        Includes any agent that is declared, has a seeded card, or has evaluations;
        excludes retired agents and pure routing utilities (no card / no evals)."""
        from sqlalchemy import text as _text
        seeded = {r["agent_id"]: dict(r) for r in db.execute(
            _text("SELECT * FROM config_agent_cards WHERE app_id = :app"), {"app": app_id}
        ).mappings().all()}
        topo  = _topo_map(db, app_id)
        stats = _agent_audit_stats(db, app_id)

        ids = set(seeded.keys())
        for aid, t in topo.items():
            if (t.get("status") or "") == "retired":
                continue
            if t.get("is_declared_agent") or aid in seeded or stats.get(aid, {}).get("evaluations", 0) > 0:
                ids.add(aid)

        def _synth(aid):
            return {"app_id": app_id, "agent_id": aid,
                    "name": aid.replace("_agent", "").replace("_", " ").title(),
                    "agent_type": "llm", "capability": None, "purpose": None,
                    "framework": None, "model_id": None, "domain": None,
                    "data_access": [], "escalation": None,
                    "allowed_actions": [], "prohibited_actions": [], "max_authority": None,
                    "kill_switch": 0, "hitl_escalation": 0, "hitl_mode": "none"}

        cards = [_build_card(seeded.get(aid) or _synth(aid), topo, stats) for aid in ids]
        cards.sort(key=lambda c: (-(c["metrics"]["evaluations"]), c["name"] or ""))
        return {"app_id": app_id, "count": len(cards), "cards": cards}

    @router.get("/v1/agent-cards/{app_id}/{agent_id}")
    def get_agent_card(app_id: str, agent_id: str, db: Session = Depends(get_db)):
        """Single agent card — used by the per-agent icon in Agent Activity."""
        from sqlalchemy import text as _text
        row = db.execute(
            _text("SELECT * FROM config_agent_cards WHERE app_id = :app AND agent_id = :aid"),
            {"app": app_id, "aid": agent_id},
        ).mappings().first()
        topo  = _topo_map(db, app_id)
        stats = _agent_audit_stats(db, app_id)
        # Synthesize a card for a registered-but-unseeded agent so the Agent-Activity
        # node icon works for ANY agent. Only 404 when the agent isn't registered at all.
        if row is None:
            if agent_id not in topo:
                raise HTTPException(status_code=404, detail=f"no such agent {app_id}/{agent_id}")
            row = {"app_id": app_id, "agent_id": agent_id,
                   "name": agent_id.replace("_agent", "").replace("_", " ").title(),
                   "agent_type": "llm", "capability": None, "purpose": None,
                   "framework": None, "model_id": None, "domain": None,
                   "data_access": [], "escalation": None,
                   "allowed_actions": [], "prohibited_actions": [], "max_authority": None,
                   "kill_switch": 0, "hitl_escalation": 0}
        return _build_card(row, topo, stats)

    @router.patch("/v1/agent-cards/{app_id}/{agent_id}")
    def update_agent_card_flags(
        app_id:    str,
        agent_id:  str,
        body:      AgentCardFlagsUpdate,
        db:        Session = Depends(get_db),
        actor:     str     = Depends(_get_actor),
    ):
        """Toggle the declarative governance flags (kill_switch, hitl_escalation) on an
        agent card. Upserts: creates a minimal config_agent_cards row for a registered-
        but-unseeded agent so the toggle persists. Writes a PapAuditLog entry. Returns
        the fully rebuilt card so the UI can re-render from the authoritative shape."""
        from core.models import AgentCard            # local import — read path uses raw SQL
        from sqlalchemy import text as _text
        if body.kill_switch is None and body.hitl_mode is None:
            raise HTTPException(status_code=400, detail="no flag provided")
        _VALID_HITL = {"none", "by_design", "human", "agent", "agent_human"}
        if body.hitl_mode is not None and body.hitl_mode not in _VALID_HITL:
            raise HTTPException(status_code=400, detail=f"hitl_mode must be one of {sorted(_VALID_HITL)}")

        row = db.query(AgentCard).filter(
            AgentCard.app_id == app_id, AgentCard.agent_id == agent_id
        ).first()

        if row is None:
            # Upsert for a registered-but-unseeded (synthesized) agent.
            if agent_id not in _topo_map(db, app_id):
                raise HTTPException(status_code=404, detail=f"no such agent {app_id}/{agent_id}")
            _mode = body.hitl_mode if body.hitl_mode is not None else "none"
            row = AgentCard(
                app_id=app_id, agent_id=agent_id,
                name=agent_id.replace("_agent", "").replace("_", " ").title(),
                agent_type="llm",
                kill_switch=bool(body.kill_switch) if body.kill_switch is not None else False,
                hitl_mode=_mode,
                hitl_escalation=(_mode != "none"),
            )
            db.add(row)
            db.flush()
            action, old_vals = "CREATE", None
        else:
            old_vals = {"kill_switch": bool(row.kill_switch),
                        "hitl_mode": row.hitl_mode or "none"}
            if body.kill_switch is not None:
                row.kill_switch = bool(body.kill_switch)
            if body.hitl_mode is not None:
                row.hitl_mode = body.hitl_mode
                row.hitl_escalation = (body.hitl_mode != "none")   # keep legacy bool in sync
            row.updated_at = datetime.utcnow()
            action = "UPDATE"

        new_vals = {"kill_switch": bool(row.kill_switch),
                    "hitl_mode": row.hitl_mode or "none"}
        db.add(PapAuditLog(
            entity_type="agent_card", entity_id=agent_id, app_id=app_id,
            action=action, actor=actor,
            old_values=json.dumps(old_vals) if old_vals is not None else None,
            new_values=json.dumps(new_vals),
        ))
        db.commit()

        topo  = _topo_map(db, app_id)
        stats = _agent_audit_stats(db, app_id)
        fresh = db.execute(
            _text("SELECT * FROM config_agent_cards WHERE app_id = :app AND agent_id = :aid"),
            {"app": app_id, "aid": agent_id},
        ).mappings().first()
        return _build_card(fresh, topo, stats)

    return router
