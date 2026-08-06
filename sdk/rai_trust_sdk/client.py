"""
TrustClient — synchronous HTTP client for the RAI Trust Platform Policy Engine.

Calls POST /v1/app/evaluate and returns a GovernanceResult.
Raises GovernanceBlock when the verdict is COERCIVE in SYNC mode.

Design notes
────────────
• Uses httpx.Client (sync) because LangChain / LangGraph enforcement must be
  synchronous — an async verdict cannot interrupt a sync execution graph.
• Transport errors are surfaced as httpx.HTTPError; callers decide whether to
  swallow or propagate.
• The client is safe to share across callback handler instances within a
  single thread.  For multi-threaded use, create one client per thread or
  wrap with a threading.Lock.

Arize / OpenInference compatibility
────────────────────────────────────
Every call to evaluate() automatically creates a GUARDRAIL span using the
global OpenTelemetry TracerProvider (set by arize.otel.register() or
phoenix.otel.register() at application startup).  When no provider is
configured the OTel API returns a no-op tracer — zero overhead, no spans.

Span attributes follow the OpenInference semantic conventions so spans appear
correctly in the Arize trace tree alongside LangChain / LangGraph spans and
are consumable by Arize evaluators (hallucination, toxicity, custom evals):

  openinference.span.kind  = "GUARDRAIL"
  input.value              = content being evaluated (raw text or JSON string)
  input.mime_type          = "text/plain" | "application/json"  (role-derived)
  output.value             = JSON: {verdict, violations, failed_evaluations}
  output.mime_type         = "application/json"
  session.id               = session_id (when present)
  metadata                 = JSON: full context (app_id, role, request_id,
                             policies, pack_trace, session_summary)

OTel status:
  COERCIVE  → StatusCode.ERROR  (appears as a failed span in Arize)
  NORMATIVE → StatusCode.OK     (surfaced through output.value only)
  PERMISSIVE→ StatusCode.OK
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

import httpx

from .identity import AgentIdentity
from .types import ContentRole, EvalMode, GovernanceBlock, GovernanceResult, Verdict

logger = logging.getLogger(__name__)

# ── OpenTelemetry / OpenInference imports (graceful no-op if absent) ──────────
# opentelemetry-api and openinference-semantic-conventions are listed in base
# dependencies so these imports should always succeed.  The try/except is a
# defensive guard against environments where only rai-trust-sdk is installed
# from source without its dependencies.

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _OtelStatusCode
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False

try:
    from openinference.semconv.trace import (
        SpanAttributes              as _OIAttrs,
        OpenInferenceSpanKindValues as _OIKind,
    )
    # Resolved attribute name constants (use spec constants, never raw strings)
    _ATTR_SPAN_KIND   = _OIAttrs.OPENINFERENCE_SPAN_KIND   # "openinference.span.kind"
    _ATTR_INPUT_VAL   = _OIAttrs.INPUT_VALUE                # "input.value"
    _ATTR_INPUT_MIME  = _OIAttrs.INPUT_MIME_TYPE            # "input.mime_type"
    _ATTR_OUTPUT_VAL  = _OIAttrs.OUTPUT_VALUE               # "output.value"
    _ATTR_OUTPUT_MIME = _OIAttrs.OUTPUT_MIME_TYPE           # "output.mime_type"
    _ATTR_SESSION_ID  = _OIAttrs.SESSION_ID                 # "session.id"
    _ATTR_METADATA    = _OIAttrs.METADATA                   # "metadata"
    _ATTR_USER_ID     = _OIAttrs.USER_ID                    # "user.id"
    _ATTR_AGENT_NAME  = _OIAttrs.AGENT_NAME                 # "agent.name"
    _ATTR_TOOL_NAME   = _OIAttrs.TOOL_NAME                  # "tool.name"
    _ATTR_TAG_TAGS    = _OIAttrs.TAG_TAGS                   # "tag.tags"
    _KIND_GUARDRAIL   = _OIKind.GUARDRAIL.value             # "GUARDRAIL"
    _OI_AVAILABLE     = True
except ImportError:  # pragma: no cover
    # Fall back to raw string literals — values are stable across spec versions.
    _ATTR_SPAN_KIND   = "openinference.span.kind"
    _ATTR_INPUT_VAL   = "input.value"
    _ATTR_INPUT_MIME  = "input.mime_type"
    _ATTR_OUTPUT_VAL  = "output.value"
    _ATTR_OUTPUT_MIME = "output.mime_type"
    _ATTR_SESSION_ID  = "session.id"
    _ATTR_METADATA    = "metadata"
    _ATTR_USER_ID     = "user.id"
    _ATTR_AGENT_NAME  = "agent.name"
    _ATTR_TOOL_NAME   = "tool.name"
    _ATTR_TAG_TAGS    = "tag.tags"
    _KIND_GUARDRAIL   = "GUARDRAIL"
    _OI_AVAILABLE     = False

# MIME type to use for each content role:
#   user/llm text → plain text;  structured agent/tool payloads → JSON
_ROLE_MIME: Dict[str, str] = {
    ContentRole.USER_PROMPT.value:    "text/plain",
    ContentRole.LLM_RESPONSE.value:   "text/plain",
    ContentRole.TOOL_CALL.value:      "application/json",
    ContentRole.AGENT_TO_AGENT.value: "application/json",
}

_RAI_TRACER_NAME = "rai-trust-sdk"


# ── Span helpers ──────────────────────────────────────────────────────────────

@contextmanager
def _guardrail_span(
    content:    str,
    role:       ContentRole,
    session_id: Optional[str],
    request_id: str,
    app_id:     str,
) -> Generator[Any, None, None]:
    """
    Context manager that wraps a governance call in an OpenInference GUARDRAIL
    span.  Yields the span (or None when OTel is not available) so the caller
    can record output attributes after the HTTP call completes.

    The span is created from the *global* TracerProvider, so it automatically
    becomes a child of whatever LangChain / LangGraph span is currently active
    on the OTel context stack — no parent plumbing required.
    """
    if not _OTEL_AVAILABLE:
        yield None
        return

    tracer = _otel_trace.get_tracer(_RAI_TRACER_NAME)
    with tracer.start_as_current_span(f"rai.guardrail.{role.value}") as span:
        # ── Input attributes (set before the HTTP call) ──────────────────────
        span.set_attribute(_ATTR_SPAN_KIND,  _KIND_GUARDRAIL)
        span.set_attribute(_ATTR_INPUT_VAL,  content)
        span.set_attribute(_ATTR_INPUT_MIME, _ROLE_MIME.get(role.value, "text/plain"))
        if session_id:
            span.set_attribute(_ATTR_SESSION_ID, session_id)
        # Seed metadata with request context — enriched with verdict detail
        # after the call completes via _record_span_output().
        span.set_attribute(
            _ATTR_METADATA,
            json.dumps({"app_id": app_id, "role": role.value, "request_id": request_id}),
        )
        yield span


def _record_span_output(span: Any, result: GovernanceResult, app_id: str, role: str) -> None:
    """
    Attach verdict detail to a completed GUARDRAIL span.

    output.value is a JSON object structured so Arize custom evaluators can
    parse it:  {"verdict": "...", "violations": [...], "failed_evaluations": [...]}

    metadata is updated with the full policy context (policies, pack_trace,
    session_summary) so engineers can inspect the complete evaluation in Arize's
    span detail panel without needing to query the audit store.
    """
    if span is None:
        return

    # ── output.value ─────────────────────────────────────────────────────────
    output_doc: Dict[str, Any] = {
        "verdict":            result.verdict.value,
        "violations":         result.violations,
        "failed_evaluations": [fe.model_dump(exclude_none=True) for fe in result.failed_evaluations],
    }
    if result.directives:
        output_doc["directives"] = [
            {"action": d.action.value, "category": d.category.value if d.category else None,
             "source": d.source.value}
            for d in result.directives
        ]
    if result.response_text:
        output_doc["response_text"] = result.response_text
    elif result.remediations:
        output_doc["remediations"] = result.remediations   # deprecated path
    span.set_attribute(_ATTR_OUTPUT_VAL,  json.dumps(output_doc))
    span.set_attribute(_ATTR_OUTPUT_MIME, "application/json")

    # ── metadata — rich context for Arize span detail ─────────────────────────
    meta: Dict[str, Any] = {
        "app_id":     app_id,
        "role":       role,
        "request_id": result.request_id,
        "verdict":    result.verdict.value,
    }
    if result.policies:
        meta["policies"] = [p.model_dump(exclude_none=True) for p in result.policies]
    if result.pack_trace:
        meta["pack_trace"] = result.pack_trace
    if result.session_summary:
        meta["session_summary"] = result.session_summary.model_dump()
    span.set_attribute(_ATTR_METADATA, json.dumps(meta))

    # ── tag.tags — list of "key:value" strings for Arize dataset filtering ───────
    # Arize uses tag.tags as a faceted list so analysts can filter/group by role,
    # verdict, or app without querying into the metadata JSON blob.
    span.set_attribute(_ATTR_TAG_TAGS, (
        f"role:{role}",
        f"verdict:{result.verdict.value}",
        f"app_id:{app_id}",
    ))

    # ── OTel status ───────────────────────────────────────────────────────────
    # COERCIVE → ERROR  so the span shows as a failure in Arize dashboards and
    # alert rules can fire on guardrail-blocked requests.
    if result.verdict == Verdict.COERCIVE:
        span.set_status(
            _OtelStatusCode.ERROR,
            f"Governance block: {'; '.join(result.violations) or 'policy violation'}",
        )
    else:
        span.set_status(_OtelStatusCode.OK)


# ── Client ────────────────────────────────────────────────────────────────────

class TrustClient:
    """
    Synchronous client for ``POST /v1/app/evaluate``.

    Parameters
    ──────────
    base_url : str
        Base URL of the Policy Engine, e.g. ``"http://localhost:3000"``.
    app_id : str
        Application identifier scoping the policy lookup.
    identity : AgentIdentity, optional
        NHI credential provider.  When supplied, ``agent_id`` is included in
        every payload and credential headers are injected automatically.
    timeout : float
        Per-request timeout in seconds (default 5.0).
    api_key : str, optional
        Bearer token for PAP / PE authentication.

    Usage
    ─────
    ::

        client = TrustClient(base_url="http://pe:3000", app_id="finapp")

        # Evaluate a user prompt (raises GovernanceBlock if COERCIVE)
        result = client.evaluate(
            content="Transfer $10,000 to account 9821",
            role=ContentRole.USER_PROMPT,
        )

        # As a context manager (closes connection pool on exit)
        with TrustClient(...) as client:
            result = client.evaluate(...)

    Arize tracing
    ─────────────
    If the host application has initialised a TracerProvider (e.g. via
    ``arize.otel.register()`` or ``phoenix.otel.register()``), every
    ``evaluate()`` call automatically emits a GUARDRAIL span.  No extra
    configuration is needed in the SDK.
    """

    def __init__(
        self,
        base_url: str,
        app_id: str,
        identity: Optional[AgentIdentity] = None,
        timeout: float = 5.0,
        api_key: Optional[str] = None,
    ) -> None:
        self._app_id   = app_id
        self._identity = identity

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if identity:
            headers.update(identity.get_headers())

        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )
        logger.debug(f"TrustClient initialised: base_url={base_url!r} app_id={app_id!r}")

    # ── Core API ───────────────────────────────────────────────────────────────

    def evaluate(
        self,
        content: str,
        role: ContentRole,
        *,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        eval_mode: EvalMode = EvalMode.SYNC,
        metadata: Optional[Dict[str, Any]] = None,
        raise_on_block: bool = True,
    ) -> GovernanceResult:
        """
        Evaluate ``content`` through the Trust Platform.

        Parameters
        ──────────
        content : str
            The text to evaluate (post-privacy-proxy sanitised).
        role : ContentRole
            Semantic role of the content.
        request_id : str, optional
            OTel trace ID.  Auto-generated (UUID4) if omitted.
        session_id : str, optional
            Multi-turn session correlation ID.  When supplied the PE accumulates
            per-dimension scores across turns.
        eval_mode : EvalMode
            ``SYNC`` (default) blocks and returns a verdict.
            ``ASYNC`` fires-and-logs; never raises GovernanceBlock.
        metadata : dict, optional
            Arbitrary caller metadata written to the audit trail only.
        raise_on_block : bool
            When ``True`` (default) and verdict is ``COERCIVE`` in SYNC mode,
            raises ``GovernanceBlock``.  Set to ``False`` to inspect the result
            without raising.

        Returns
        ───────
        GovernanceResult

        Raises
        ──────
        GovernanceBlock
            When ``raise_on_block=True``, ``eval_mode=SYNC``, and the PE
            returns ``COERCIVE``.
        httpx.HTTPStatusError
            On 4xx/5xx responses from the PE.
        httpx.TimeoutException
            When the PE does not respond within ``timeout`` seconds.
        """
        rid = request_id or str(uuid.uuid4())

        # Always include 'source' for audit trail and backward-compatible context
        # routing (apps not yet in app_stacks fall back to context_routing.yaml).
        # For registered apps, pack activation is driven by the app_stack assignment
        # in the PE — not by this field.  Caller-supplied metadata is merged in
        # so tool_name, agent_id, etc. are preserved.
        merged_metadata: Dict[str, Any] = {"source": self._app_id}
        if metadata:
            merged_metadata.update(metadata)

        payload: Dict[str, Any] = {
            "app_id":     self._app_id,
            "request_id": rid,
            "role":       role.value,
            "content":    content,
            "eval_mode":  eval_mode.value,
            "metadata":   merged_metadata,
        }
        if session_id:
            payload["session_id"] = session_id
        if self._identity:
            payload["agent_id"] = self._identity.agent_id

        logger.debug(f"TrustClient.evaluate role={role.value} request_id={rid}")

        # ── GUARDRAIL span wraps the HTTP call ────────────────────────────────
        # _guardrail_span() resolves to a no-op context manager when no
        # TracerProvider is configured, so there is no performance cost unless
        # the host application has explicitly initialised OTel.
        with _guardrail_span(content, role, session_id, rid, self._app_id) as span:
            response = self._http.post("/v1/app/evaluate", json=payload)
            response.raise_for_status()

            result = GovernanceResult.model_validate(response.json())
            logger.debug(
                f"TrustClient verdict={result.verdict.value} "
                f"violations={result.violations} request_id={rid}"
            )

            # Record output attributes on the span before it closes
            _record_span_output(span, result, self._app_id, role.value)

            # ── Agent / tool context from caller metadata ─────────────────────
            # These are first-class OpenInference attributes so Arize can filter
            # and group spans by agent or tool without parsing the metadata blob.
            if span is not None and metadata:
                _agent = metadata.get("agent_name")
                if _agent:
                    span.set_attribute(_ATTR_AGENT_NAME, str(_agent))
                _tool = metadata.get("tool_name")
                if _tool:
                    span.set_attribute(_ATTR_TOOL_NAME, str(_tool))

        if raise_on_block and result.verdict == Verdict.COERCIVE and eval_mode == EvalMode.SYNC:
            raise GovernanceBlock(result)

        return result

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> "TrustClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"TrustClient(base_url={self._http.base_url!r}, "
            f"app_id={self._app_id!r})"
        )
