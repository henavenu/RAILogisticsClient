"""
instrument() — zero-touch governance wrapper for compiled LangGraph apps.

The entire SDK integration for a LangGraph application is:

    from rai_trust_sdk.langgraph import instrument

    app = instrument(app, base_url="http://pe:3000", app_id="myapp")

    # With REDACT (default) — no exception handling needed:
    result = app.invoke(state)

    # With RAISE (opt-in) — full block detail available:
    from rai_trust_sdk import GovernanceBlock
    try:
        result = app.invoke(state)
    except GovernanceBlock as e:
        print(e.result.violations)

Three governance layers are injected automatically — no changes to nodes,
tools, or graph definition required:

  Layer 1 · USER_PROMPT  — evaluated before the graph runs
            Catches prompt injection before any agent logic executes.

  Layer 2 · TOOL_CALL    — evaluated before sensitive tools execute
            Catches sanctions hits, AML patterns, and policy violations
            at the moment of execution, not just at intent time.

  Layer 3 · LLM_RESPONSE — evaluated after the graph completes
            Catches PII leakage and hallucinated data before delivery.

EnforcementMode controls what happens on a COERCIVE verdict:

  NONE    — log only, execution continues unchanged (shadow/audit mode)
  REDACT  — substitute a safe refusal message; no exception raised (default)
  RAISE   — raise GovernanceBlock; application must catch it (opt-in)
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID

from ..client import TrustClient
from ..types import (
    ContentRole,
    DirectiveAction,
    EnforcementMode,
    EvalMode,
    GovernanceBlock,
    GovernanceResult,
    HITLInterrupt,
    Verdict,
    _DIRECTIVE_PRIORITY,
)

# ── LangChain BaseCallbackHandler (optional but strongly recommended) ──────────
# Inheriting from BaseCallbackHandler ensures that LangChain's CallbackManager
# registers this handler into self.handlers for ALL event types — including
# on_llm_end, which is dispatched through CallbackManager.on_chat_model_start()
# and requires a proper BaseCallbackHandler instance.
#
# Without inheritance, on_tool_start works (BaseTool dispatches via duck typing)
# but on_llm_end is silently dropped, so token counts never accumulate.
try:
    from langchain_core.callbacks import BaseCallbackHandler as _LCBaseHandler
    _CALLBACK_BASE: Any = _LCBaseHandler
except ImportError:
    _CALLBACK_BASE = object  # Graceful degradation: token counting disabled

logger = logging.getLogger(__name__)

# Matches snake_case node names used in LangGraph (e.g. router, intl_wire_agent)
# Excludes CamelCase LangChain internals (ChatGroq, RunnableSequence, etc.)
_NODE_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')

# Safe refusal text injected when REDACT mode blocks content.
_REDACT_COERCIVE = (
    "I'm sorry, I'm unable to process that request as it doesn't comply "
    "with our policies. Please contact support if you believe this is an error."
)


# ── Public entry point ────────────────────────────────────────────────────────

def instrument(
    graph: Any,
    *,
    base_url: str,
    app_id: str,
    api_key: Optional[str] = None,
    tool_filter: Optional[List[str]] = None,
    agent_nodes: Optional[List[str]] = None,
    a2a_tools: Optional[Dict[str, str]] = None,
    eval_mode: EvalMode = EvalMode.SYNC,
    enforcement_mode: EnforcementMode = EnforcementMode.REDACT,
    on_result: Optional[Callable[[GovernanceResult, ContentRole], None]] = None,
    timeout: float = 5.0,
    otel_endpoint: Optional[str] = None,
    otel_protocol: str = "grpc",
    privacy_proxy: Optional[Any] = None,
    hitl_on_normative_a2a: bool = False,
    hitl_normative_threshold: int = 2,
) -> "GovernedGraph":
    """
    Wrap a compiled LangGraph app with RAI Trust Platform governance.

    Parameters
    ──────────
    graph
        A compiled LangGraph ``StateGraph`` (the object returned by
        ``graph.compile()``).
    base_url : str
        Base URL of the Policy Engine, e.g. ``"http://localhost:3000"``.
    app_id : str
        Application identifier used to scope policy pack selection.
    api_key : str, optional
        Bearer token for Trust Platform authentication.
    tool_filter : list[str], optional
        When provided, Layer 2 evaluation is restricted to only these tool
        names.  When omitted (default), ALL tool calls are evaluated.
    agent_nodes : list[str], optional
        Names of LangGraph nodes whose working context should be evaluated
        as ``AGENT_TO_AGENT`` when they start executing.  Used for
        ``on_chain_start``-based detection (LangGraph version-dependent).
        Pair with ``a2a_tools`` for guaranteed detection across all versions.
        Example: ``agent_nodes=["intl_wire_agent"]``.
    a2a_tools : dict[str, str], optional
        Mapping of ``{tool_name: agent_name}`` that identifies which tools
        represent an agent-to-agent handoff point.  When a listed tool fires,
        an ``AGENT_TO_AGENT`` evaluation is triggered **before** the normal
        ``TOOL_CALL`` evaluation using the tool's input as the working context.
        This is the most reliable A2A trigger across all LangGraph versions
        because it hooks into ``on_tool_start``, which is guaranteed to fire.
        Example: ``a2a_tools={"international_transfer": "intl_wire_agent"}``.
    eval_mode : EvalMode
        ``SYNC`` (default): wait for verdict before proceeding.
        ``ASYNC``: fire-and-log, enforcement_mode is ignored.
    enforcement_mode : EnforcementMode
        Controls what happens when a COERCIVE verdict is returned.

        ``REDACT`` (default): substitute a safe refusal message transparently —
        no exception raised, no error handling required in application code.

        ``RAISE``: raise ``GovernanceBlock`` on COERCIVE verdicts.
        Use this when your application wants to display rich block details
        (violations list, verdict, request_id) in the UI.

        ``NONE``: log only — governance has no runtime effect (shadow mode).

    on_result : callable, optional
        ``on_result(result: GovernanceResult, role: ContentRole)`` — hook for
        logging, metrics, or OTEL span recording after each evaluation.
    timeout : float
        Per-request HTTP timeout in seconds (default 5.0).
    privacy_proxy : PrivacyProxy, optional
        When provided, every user prompt is run through the proxy **before**
        reaching the Policy Engine.  Stage 1 (pattern match + entity
        tokenization) and Stage 2 (local embedding classifier) both execute
        entirely in-process — no content is sent externally.  A blocked
        result short-circuits all further evaluation and returns a safe
        refusal without any PE call.

    Returns
    ───────
    ``GovernedGraph`` — a drop-in replacement for the original compiled graph.
    Supports ``.invoke()``, ``.stream()``, and ``.get_graph()``.

    Example — REDACT mode (default, zero exception-handling boilerplate)
    ────────────────────────────────────────────────────────────────────
    ::

        from rai_trust_sdk.langgraph import instrument

        app = instrument(
            app,
            base_url="http://localhost:3000",
            app_id="mybank",
        )

        # Blocked requests return a safe refusal message automatically.
        result = app.invoke(state)

    Example — RAISE mode (opt-in, rich block detail in UI)
    ──────────────────────────────────────────────────────
    ::

        from rai_trust_sdk.langgraph import instrument
        from rai_trust_sdk import GovernanceBlock, EnforcementMode

        app = instrument(
            app,
            base_url="http://localhost:3000",
            app_id="mybank",
            enforcement_mode=EnforcementMode.RAISE,
        )

        try:
            result = app.invoke(state)
        except GovernanceBlock as e:
            return {"blocked": True, "violations": e.result.violations}
    """
    client = TrustClient(
        base_url=base_url,
        app_id=app_id,
        api_key=api_key,
        timeout=timeout,
    )

    # OTel — set up when either an OTLP endpoint or a file export path is given.
    # OTEL_FILE_EXPORT_PATH env var enables file-only export without a Phoenix endpoint —
    # useful for local inspection before a collector is available.
    tracer = None
    _file_path = os.environ.get("OTEL_FILE_EXPORT_PATH")
    if otel_endpoint or _file_path:
        try:
            from ..observability import setup_otel
            # x-phoenix-project-name groups all traces for this app under one
            # project tile in the Phoenix dashboard automatically.
            tracer = setup_otel(
                app_id,
                endpoint=otel_endpoint,   # may be None when file-only
                protocol=otel_protocol,
                headers={"x-phoenix-project-name": app_id} if otel_endpoint else {},
                file_export_path=_file_path,
            )
            logger.info(
                "RAI Trust: OTel configured → endpoint=%s file=%s",
                otel_endpoint or "—", _file_path or "—",
            )
        except ImportError:
            logger.warning(
                "RAI Trust: OTel requested but opentelemetry packages are not installed. "
                "Install with: pip install 'rai-trust-sdk[otel]'"
            )

    # Expand agent_nodes to match both the node label AND the Python function
    # name.  LangGraph may fire on_chain_start with either:
    #   - the label passed to add_node()  → "intl_wire_agent"
    #   - the wrapped function __name__   → "intl_wire_agent_node"
    # We normalise both sides at instrument() time so no runtime lookup needed.
    expanded_nodes: Optional[Set[str]] = None
    if agent_nodes:
        expanded_nodes = set()
        for n in agent_nodes:
            expanded_nodes.add(n)
            if n.endswith("_node"):
                expanded_nodes.add(n[:-5])       # strip _node  (label → func)
            else:
                expanded_nodes.add(f"{n}_node")  # add   _node  (func → label)

    return GovernedGraph(
        graph=graph,
        client=client,
        app_id=app_id,
        tool_filter=tool_filter,
        agent_nodes=expanded_nodes,
        a2a_tools=dict(a2a_tools) if a2a_tools else None,
        eval_mode=eval_mode,
        enforcement_mode=enforcement_mode,
        on_result=on_result,
        tracer=tracer,
        privacy_proxy=privacy_proxy,
        hitl_on_normative_a2a=hitl_on_normative_a2a,
        hitl_normative_threshold=hitl_normative_threshold,
    )


# ── GovernedGraph ─────────────────────────────────────────────────────────────

class GovernedGraph:
    """
    Drop-in wrapper around a compiled LangGraph that enforces governance on
    every invocation.

    Do not instantiate directly — use ``instrument()`` instead.
    """

    def __init__(
        self,
        graph: Any,
        client: TrustClient,
        app_id: str,
        tool_filter: Optional[List[str]],
        agent_nodes: Optional[Set[str]],
        a2a_tools: Optional[Dict[str, str]],
        eval_mode: EvalMode,
        enforcement_mode: EnforcementMode,
        on_result: Optional[Callable[[GovernanceResult, ContentRole], None]],
        tracer: Any = None,
        privacy_proxy: Any = None,
        hitl_on_normative_a2a: bool = False,
        hitl_normative_threshold: int = 2,
    ) -> None:
        self._graph            = graph
        self._client           = client
        self._app_id           = app_id
        self._tool_filter      = set(tool_filter) if tool_filter else None
        self._agent_nodes      = agent_nodes   # nodes → A2A via on_chain_start
        self._a2a_tools        = a2a_tools     # tools → A2A via on_tool_start (guaranteed)
        self._eval_mode        = eval_mode
        self._enforcement_mode = enforcement_mode
        self._on_result        = on_result
        self._tracer           = tracer
        self._privacy_proxy    = privacy_proxy

        # ── HITL: human-in-the-loop on NORMATIVE A2A verdicts ────────────────
        # When hitl_on_normative_a2a=True, NORMATIVE A2A verdicts increment a
        # per-session counter.  Once the counter reaches hitl_normative_threshold,
        # HITLInterrupt is raised so the server can pause and route the request
        # for human review.
        # _hitl_normative_counts and _hitl_cleared_sessions are shared by reference
        # with _ToolGuardCallback so they persist across invocations.
        self._hitl_on_normative_a2a: bool       = hitl_on_normative_a2a
        self._hitl_normative_threshold: int      = hitl_normative_threshold
        self._hitl_normative_counts: Dict[str, int] = {}   # session_id → NORMATIVE count
        self._hitl_cleared_sessions: Set[str]       = set()  # HITL-approved sessions (skip check)

        # ── Option A: graph topology discovery ───────────────────────────────
        # Extract all node names from the compiled graph at startup — before any
        # traffic flows.  This gives structural completeness regardless of which
        # code paths are exercised at runtime.  Internal LangGraph nodes
        # (__start__, __end__) are filtered out; only user-defined nodes remain.
        self._discovered_nodes: List[str] = []
        try:
            raw_nodes = list(graph.nodes.keys())
            self._discovered_nodes = [n for n in raw_nodes if not n.startswith("__")]
            logger.info(
                "GovernedGraph [%s]: discovered %d nodes from graph topology: %s",
                app_id, len(self._discovered_nodes), self._discovered_nodes,
            )
        except Exception as exc:
            logger.debug("GovernedGraph: node topology discovery skipped (%s)", exc)

        # ── Agent registry — fire-and-forget registration with PAP ───────────
        # POST /internal/register-agents so the dashboard shows declared agents
        # even before any eval records exist.  Runs in a daemon thread so it
        # never blocks graph instantiation or the first invoke() call.
        # PAP URL: RAI_PAP_URL env var, default http://localhost:3002.
        self._pap_url: str = os.environ.get("RAI_PAP_URL", "http://localhost:3002")
        if self._discovered_nodes:
            self._register_agents_async()

    # ── Main invoke ───────────────────────────────────────────────────────────

    def invoke(self, state: Any, config: Optional[Dict] = None, **kwargs: Any) -> Any:
        """
        Governed replacement for ``compiled_graph.invoke()``.

        When OTel is configured, creates a root ``rai.governance.turn`` span
        so that Layer 1, 2 and 3 child spans appear as a waterfall in Phoenix.
        """
        session_id  = _extract_session_id(state)
        customer_id = _extract_customer_id(state)

        if self._tracer:
            with self._tracer.start_as_current_span("rai.governance.turn") as root:
                # OpenInference span kind — "CHAIN" tells Arize/Phoenix this is an
                # orchestration wrapper, not an LLM call.  Without this the span is
                # categorised as UNKNOWN and excluded from most built-in views.
                root.set_attribute("openinference.span.kind", "CHAIN")
                root.set_attribute("graph.node.name",         "rai.governance.turn")
                root.set_attribute("rai.app_id",              self._client._app_id)
                if session_id:
                    root.set_attribute("session.id", session_id)  # OI canonical attr
                    root.set_attribute("rai.session_id", session_id)
                if customer_id:
                    root.set_attribute("user.id", customer_id)    # Arize user attribution
                return self._invoke_governed(state, config, session_id, **kwargs)
        return self._invoke_governed(state, config, session_id, **kwargs)

    def _invoke_governed(
        self,
        state: Any,
        config: Optional[Dict],
        session_id: Optional[str],
        **kwargs: Any,
    ) -> Any:
        """Inner invoke — three governance layers, called from invoke()."""

        # ── Privacy Proxy — shadow-copy pattern ──────────────────────────────────
        # The proxy produces a sanitised EVALUATION COPY used only for the PE
        # call and the audit trail.  The LangGraph execution state is never
        # modified: the LLM and tool execution path always receive the original
        # user text so that tool calls (e.g. domestic_transfer) can use the real
        # routing/account numbers.
        #
        # Why shadow-copy and not state replacement?
        #   Routing numbers and bank account numbers are GLBA NPI / NACHA-
        #   protected data.  They must not appear in requests to external
        #   Guardrail Service providers or in the governance audit trail.  But
        #   tokenising them in the LangGraph state would break tool execution —
        #   the LLM would pass "[ROUTING_NUMBER]" to the tool instead of the
        #   real 9-digit ABA number.  The shadow copy satisfies both constraints.
        user_text = _extract_last_human(state)
        eval_text = user_text   # default: PE evaluates original when no proxy
        _pii_meta: Optional[Dict[str, Any]] = None
        if user_text and self._privacy_proxy is not None:
            proxy_result = self._privacy_proxy.process(user_text)
            if proxy_result.anonymized and proxy_result.anonymized != user_text:
                eval_text = proxy_result.anonymized   # PE/audit sees tokens
                _pii_meta = {
                    "pii_detected":   proxy_result.detections,
                    "pii_anonymized": True,
                }
                logger.info(
                    "GovernedGraph: PrivacyProxy — PII masked before PE evaluation, "
                    "detections=%s", proxy_result.detections
                )
                # state is intentionally NOT modified — LLM sees original

        # ── Layer 1: User input guard ─────────────────────────────────────────
        if eval_text:
            logger.debug("GovernedGraph: Layer 1 — evaluating USER_PROMPT")
            result = self._evaluate(eval_text, ContentRole.USER_PROMPT, session_id,
                                    metadata=_pii_meta)
            if result is not None:
                _raise_if_hitl_directive(result, session_id)
                blocked = self._enforce(result, ContentRole.USER_PROMPT)
                if blocked is not None:
                    # Graph was not invoked — return synthetic state with refusal
                    return _inject_ai_message(state, blocked)

        # ── Layer 2: Tool pre-execution guard (injected via callback) ─────────
        merged_config, tool_cb = self._build_config(config, session_id)

        # ── Run the graph ─────────────────────────────────────────────────────
        try:
            output = self._graph.invoke(state, config=merged_config, **kwargs)
        except HITLInterrupt:
            # HITL interrupt — human review required for NORMATIVE A2A.
            # Always re-raise regardless of enforcement_mode so the application
            # server can implement the pause/review/resume workflow.
            raise
        except GovernanceBlock as exc:
            # Layer 2 fired GovernanceBlock from inside the tool callback.
            # In REDACT mode: absorb it and return the policy remediation as the
            # response (context-specific, template-rendered by the PE).
            # In RAISE mode: re-raise so the application can handle it.
            if self._enforcement_mode == EnforcementMode.REDACT:
                msg = (
                    exc.result.response_text
                    or (exc.result.remediations[0] if exc.result.remediations else None)
                    or _REDACT_COERCIVE
                )
                logger.info(
                    "GovernedGraph: Layer 2 REDACT — tool blocked, returning policy "
                    "remediation. violations=%s", exc.result.violations
                )
                return _inject_ai_message(state, msg)
            raise  # RAISE mode — propagate to caller

        # ── Propagate token counts to the active OTel root span ──────────────
        # tool_cb._token_usage is populated by on_llm_end during graph execution.
        # Setting these as span attributes makes them available in Phoenix / Arize
        # for cost analysis (Tier 2 metrics) and per-model attribution.
        _record_token_usage(tool_cb)

        # ── Close any open node spans ─────────────────────────────────────────
        # Node spans stay open across LLM calls within a node so that tool spans
        # nest correctly.  Close them now that graph execution is complete.
        # Must happen BEFORE Layer 3 so the LLM_RESPONSE guardrail span is a
        # sibling of the agent spans (under the root turn), not nested inside a
        # dangling node span.
        tool_cb._close_all_node_spans()

        # ── Layer 3: Response guard ───────────────────────────────────────────
        # The proxy is applied to the LLM response for PE evaluation only —
        # exactly the same shadow-copy pattern used in Layer 1.  If the LLM
        # echoes back user PII (routing numbers, account numbers) the PE should
        # see tokens, not raw digits, so data_leakage never fires on legitimate
        # transaction confirmations.  The actual response returned to the user
        # is NOT modified here — only the eval_text sent to the PE is masked.
        response_text = _extract_last_ai(output)
        if response_text:
            logger.debug("GovernedGraph: Layer 3 — evaluating LLM_RESPONSE")
            eval_response_text = response_text
            if self._privacy_proxy is not None:
                _resp_proxy = self._privacy_proxy.process(response_text)
                if _resp_proxy.anonymized and _resp_proxy.anonymized != response_text:
                    eval_response_text = _resp_proxy.anonymized
                    logger.info(
                        "GovernedGraph: PrivacyProxy — LLM response PII masked "
                        "before PE evaluation, detections=%s", _resp_proxy.detections
                    )
            # Pass the generating node name so the audit trail can attribute
            # llm_response evals to the correct agent (same field read by PAP via
            # json_extract(metadata_json, '$.agent_name')).
            _l3_meta = {"agent_name": tool_cb._current_node} if tool_cb._current_node else None
            result = self._evaluate(eval_response_text, ContentRole.LLM_RESPONSE, session_id,
                                    metadata=_l3_meta)
            if result is not None:
                _raise_if_hitl_directive(result, session_id)
                blocked = self._enforce(result, ContentRole.LLM_RESPONSE)
                if blocked is not None:
                    # Replace the LLM output with the safe refusal inline
                    return _replace_last_ai(output, blocked)

        return output

    def stream(self, state: Any, config: Optional[Dict] = None, **kwargs: Any):
        """
        Governed replacement for ``compiled_graph.stream()``.

        Layer 1 fires before streaming begins; Layer 3 fires when the stream
        is consumed (requires iterating the generator to completion).
        Layer 2 fires inside the stream via the tool callback.
        """
        session_id = _extract_session_id(state)

        # Layer 1 — same shadow-copy pattern as invoke(): PE sees tokenised text,
        # state is never modified so the streamed graph receives original values.
        user_text = _extract_last_human(state)
        eval_text = user_text
        _pii_meta: Optional[Dict[str, Any]] = None
        if user_text and self._privacy_proxy is not None:
            proxy_result = self._privacy_proxy.process(user_text)
            if proxy_result.anonymized and proxy_result.anonymized != user_text:
                eval_text = proxy_result.anonymized
                _pii_meta = {
                    "pii_detected":   proxy_result.detections,
                    "pii_anonymized": True,
                }
                logger.info(
                    "GovernedGraph.stream: PrivacyProxy — PII masked before PE evaluation, "
                    "detections=%s", proxy_result.detections,
                )
        if eval_text:
            result = self._evaluate(eval_text, ContentRole.USER_PROMPT, session_id,
                                    metadata=_pii_meta)
            if result is not None:
                _raise_if_hitl_directive(result, session_id)
                blocked = self._enforce(result, ContentRole.USER_PROMPT)
                if blocked is not None:
                    yield _inject_ai_message(state, blocked)
                    return

        merged_config, tool_cb = self._build_config(config, session_id)

        # Stream — Layer 3 on final chunk
        last_output: Any = None
        try:
            for chunk in self._graph.stream(state, config=merged_config, **kwargs):
                last_output = chunk
                yield chunk
        except GovernanceBlock as exc:
            tool_cb._close_all_node_spans()
            if self._enforcement_mode == EnforcementMode.REDACT:
                msg = (
                    exc.result.response_text
                    or (exc.result.remediations[0] if exc.result.remediations else None)
                    or _REDACT_COERCIVE
                )
                yield _inject_ai_message(state, msg)
                return
            raise

        if last_output is not None:
            _record_token_usage(tool_cb)
            tool_cb._close_all_node_spans()
            response_text = _extract_last_ai(last_output)
            if response_text:
                eval_response_text = response_text
                if self._privacy_proxy is not None:
                    _resp_proxy = self._privacy_proxy.process(response_text)
                    if _resp_proxy.anonymized and _resp_proxy.anonymized != response_text:
                        eval_response_text = _resp_proxy.anonymized
                        logger.info(
                            "GovernedGraph.stream: PrivacyProxy — LLM response PII "
                            "masked before PE evaluation, detections=%s",
                            _resp_proxy.detections,
                        )
                _l3_meta = {"agent_name": tool_cb._current_node} if tool_cb._current_node else None
                result = self._evaluate(eval_response_text, ContentRole.LLM_RESPONSE, session_id,
                                        metadata=_l3_meta)
                if result is not None:
                    _raise_if_hitl_directive(result, session_id)
                    blocked = self._enforce(result, ContentRole.LLM_RESPONSE)
                    if blocked is not None:
                        yield _replace_last_ai(last_output, blocked)

    # ── Pass-through for graph inspection ────────────────────────────────────

    def get_graph(self, *args: Any, **kwargs: Any) -> Any:
        return self._graph.get_graph(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Proxy any other attribute to the underlying compiled graph."""
        return getattr(self._graph, name)

    # ── Agent registry ────────────────────────────────────────────────────────

    def _register_agents_async(self) -> None:
        """
        POST agent topology to PAP /internal/register-agents in a daemon thread.

        Fire-and-forget: never blocks graph instantiation.
        Failures are silently swallowed (PAP may not be running in tests/CI).

        Builds:
          agent_nodes — declared agent names (base names without _node suffix,
                        deduplicated — GovernedGraph stores expanded variants).
          a2a_tools   — original {tool_name: agent_name} mapping.
        """
        import threading

        # Reconstruct base agent names from the expanded set.
        # instrument() expands "intl_wire_agent" → {"intl_wire_agent", "intl_wire_agent_node"}.
        # We send only base names (without _node) for cleaner registry entries.
        base_agent_nodes: List[str] = []
        if self._agent_nodes:
            seen: Set[str] = set()
            for n in self._agent_nodes:
                base = n[:-5] if n.endswith("_node") else n
                if base not in seen:
                    seen.add(base)
                    base_agent_nodes.append(base)

        payload_data = {
            "app_id":           self._app_id,
            "discovered_nodes": self._discovered_nodes,
            "agent_nodes":      base_agent_nodes,
            "a2a_tools":        self._a2a_tools or {},
        }

        def _post() -> None:
            try:
                import json as _json
                import urllib.request as _urlreq
                payload_bytes = _json.dumps(payload_data).encode("utf-8")
                req = _urlreq.Request(
                    f"{self._pap_url}/internal/register-agents",
                    data=payload_bytes,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _urlreq.urlopen(req, timeout=3) as resp:
                    logger.info(
                        "GovernedGraph [%s]: registered %d nodes with PAP (HTTP %s)",
                        self._app_id, len(self._discovered_nodes), resp.status,
                    )
            except Exception as exc:
                # Non-critical — PAP may not be running during unit tests / CI
                logger.debug(
                    "GovernedGraph [%s]: agent registration skipped — %s",
                    self._app_id, exc,
                )

        threading.Thread(target=_post, daemon=True).start()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        content: str,
        role: ContentRole,
        session_id: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[GovernanceResult]:
        """
        Call the Policy Engine.  Returns None if evaluation is ASYNC or if
        the PE is unreachable (fail-open).  Never raises GovernanceBlock here —
        enforcement decisions are made by _enforce().

        Parameters
        ──────────
        metadata : dict, optional
            Extra metadata forwarded to the PE audit trail.  Used to surface
            Privacy Proxy detections as ``pii_detected`` / ``pii_anonymized``
            fields so the PAP audit log can show the "🔏 pii masked" badge.

        OTel note
        ─────────
        TrustClient.evaluate() always creates a GUARDRAIL child span internally
        via _guardrail_span() using the global TracerProvider.  Because the root
        ``rai.governance.turn`` span is active on the OTel context stack (set in
        invoke()), the client span automatically becomes its child — no extra
        wrapping needed here.  Adding a GovernanceSpan wrapper here would produce
        two nested spans with identical names and attributes (the client span
        inside the wrapper span), corrupting the Phoenix / Arize trace tree.
        """
        try:
            request_id = str(uuid.uuid4())
            result = self._client.evaluate(
                content=content,
                role=role,
                request_id=request_id,
                session_id=session_id,
                eval_mode=self._eval_mode,
                raise_on_block=False,
                metadata=metadata,
            )

            if self._on_result:
                try:
                    self._on_result(result, role)
                except Exception:
                    pass
            return result
        except Exception as exc:
            logger.warning(
                "GovernedGraph: PE unreachable or error — failing open. "
                "role=%s error=%s", role.value, exc
            )
            return None  # Fail-open: governance silently bypassed on PE outage

    def _enforce(
        self,
        result: GovernanceResult,
        role: ContentRole,
    ) -> Optional[str]:
        """
        Apply enforcement policy to a GovernanceResult.

        Returns a substitute message string if the content should be replaced
        (REDACT mode, COERCIVE verdict), or raises GovernanceBlock (RAISE mode).
        Returns None when execution should continue unchanged.
        """
        if result.verdict != Verdict.COERCIVE:
            if result.verdict == Verdict.NORMATIVE:
                logger.info(
                    "GovernedGraph: NORMATIVE verdict — role=%s violations=%s",
                    role.value, result.violations,
                )
            return None   # PERMISSIVE or NORMATIVE — proceed

        # COERCIVE verdict below
        logger.warning(
            "GovernedGraph: COERCIVE verdict — role=%s enforcement=%s violations=%s",
            role.value, self._enforcement_mode.value, result.violations,
        )

        if self._enforcement_mode == EnforcementMode.RAISE:
            raise GovernanceBlock(result)

        if self._enforcement_mode == EnforcementMode.REDACT:
            # Prefer response_text (PE-rendered, context-aware refusal via MessageWriter).
            # Fall back to legacy remediations[0], then the generic safe refusal.
            return (
                result.response_text
                or (result.remediations[0] if result.remediations else None)
                or _REDACT_COERCIVE
            )

        # NONE — log only, return None so execution continues
        return None

    def _build_config(
        self,
        config: Optional[Dict],
        session_id: Optional[str],
    ) -> tuple:
        """
        Merge user config with the Layer 2 tool guard callback.

        Returns ``(merged_config_dict, tool_cb)`` so callers can read
        accumulated token usage from the callback after graph execution.
        """
        merged = dict(config or {})
        tool_cb = _ToolGuardCallback(
            client=self._client,
            app_id=self._app_id,
            tool_filter=self._tool_filter,
            agent_nodes=self._agent_nodes,
            a2a_tools=self._a2a_tools,
            session_id=session_id,
            eval_mode=self._eval_mode,
            enforcement_mode=self._enforcement_mode,
            on_result=self._on_result,
            tracer=self._tracer,
            hitl_on_normative_a2a=self._hitl_on_normative_a2a,
            hitl_normative_threshold=self._hitl_normative_threshold,
            hitl_normative_counts=self._hitl_normative_counts,   # shared by reference
            hitl_cleared_sessions=self._hitl_cleared_sessions,   # shared by reference
        )
        existing = merged.get("callbacks", [])
        merged["callbacks"] = list(existing) + [tool_cb]
        return merged, tool_cb


# ── Layer 2: Tool callback ────────────────────────────────────────────────────

class _ToolGuardCallback(_CALLBACK_BASE):
    """
    LangChain callback that intercepts tool and LLM lifecycle events to
    enforce governance policies via the RAI Trust Platform.

    Inherits from BaseCallbackHandler so LangChain's CallbackManager registers
    this handler for ALL event types — including on_llm_end (token counting)
    and on_tool_start (tool-call governance and A2A evaluation).

    Without BaseCallbackHandler inheritance, on_tool_start fires (BaseTool uses
    duck typing) but on_llm_end is silently dropped by CallbackManager, causing
    token counts to never accumulate.
    """

    # raise_error=True: when on_tool_start or on_chain_start raises GovernanceBlock
    # or HITLInterrupt, LangChain's CallbackManager re-raises it instead of
    # swallowing it.  Without this, both exceptions are caught and logged as
    # warnings and the tool executes regardless of the governance verdict.
    raise_error        = True
    ignore_llm         = False
    ignore_chain       = False
    ignore_agent       = False
    ignore_retriever   = False
    ignore_chat_model  = False
    ignore_custom_event = False

    def __init__(
        self,
        client: TrustClient,
        app_id: str,
        tool_filter: Optional[Set[str]],
        agent_nodes: Optional[Set[str]],
        a2a_tools: Optional[Dict[str, str]],
        session_id: Optional[str],
        eval_mode: EvalMode,
        enforcement_mode: EnforcementMode,
        on_result: Optional[Callable],
        tracer: Any = None,
        hitl_on_normative_a2a: bool = False,
        hitl_normative_threshold: int = 2,
        hitl_normative_counts: Optional[Dict[str, int]] = None,
        hitl_cleared_sessions: Optional[Set[str]] = None,
    ) -> None:
        # Initialise BaseCallbackHandler (sets up internal run tracking dicts).
        # Use try/except so the SDK still works if langchain_core is unavailable.
        try:
            super().__init__()
        except Exception:
            pass

        self._client           = client
        self._app_id           = app_id
        self._tool_filter      = tool_filter
        self._agent_nodes      = agent_nodes   # on_chain_start-based A2A (version-dependent)
        self._a2a_tools        = a2a_tools     # on_tool_start-based A2A (guaranteed)
        self._session_id       = session_id
        self._eval_mode        = eval_mode
        self._enforcement_mode = enforcement_mode
        self._on_result        = on_result
        self._tracer           = tracer        # OTel tracer (None = no spans)

        # HITL state (shared references from GovernedGraph — persist across turns)
        self._hitl_on_normative_a2a    = hitl_on_normative_a2a
        self._hitl_normative_threshold = hitl_normative_threshold
        self._hitl_normative_counts    = hitl_normative_counts if hitl_normative_counts is not None else {}
        self._hitl_cleared_sessions    = hitl_cleared_sessions if hitl_cleared_sessions is not None else set()

        # ── Runtime state (updated during graph execution) ────────────────
        self._current_node: Optional[str] = None
        self._current_model: Optional[str] = None   # captured in on_chat_model_start
        # token_usage[node_name] = {prompt_tokens, completion_tokens, total_tokens, model}
        self._token_usage: Dict[str, Dict[str, Any]] = {}
        # OTel span context managers:
        # _active_node_spans  — keyed by node name; a node span stays open until
        #                       the NEXT different node starts (or turn ends).
        #                       Value is (span, ctx_mgr) tuple so we can fall back
        #                       to span.end() if the context manager's __exit__
        #                       fails across LangGraph copy_context() boundaries.
        # _node_span_ctxs     — keyed by LangChain run_id (on_chain_start path,
        #                       kept for compatibility when on_chain_start fires).
        # _tool_span_ctxs     — keyed by LangChain run_id (on_tool_start/end path).
        self._active_node_spans: Dict[str, Any] = {}  # node_name → (span, ctx_mgr)
        self._node_span_ctxs:   Dict[UUID, Any] = {}  # run_id → ctx_mgr
        self._tool_span_ctxs:   Dict[UUID, Any] = {}  # run_id → ctx_mgr

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Fire BEFORE a tool executes."""
        tool_name = serialized.get("name", "")

        # ── OTel tool span ────────────────────────────────────────────────────
        # Opens a TOOL span as a child of the active node span.
        # Tool spans use start_as_current_span (unlike node spans) because
        # on_tool_start and on_tool_end fire within the same BaseTool.invoke()
        # call, so they are always in the same copy_context — no boundary issue.
        # Span is stored in _tool_span_ctxs and closed in on_tool_end /
        # on_tool_error.  If GovernanceBlock is raised below (tool blocked),
        # the span is closed inline before re-raising.
        if self._tracer:
            try:
                # Use the node span's context as explicit parent so the tool
                # span nests correctly even though the node span was created
                # via start_span() (not start_as_current_span).
                node_entry = self._active_node_spans.get(self._current_node or "")
                parent_ctx = node_entry[1] if node_entry else None  # (span, ctx)[1]

                ctx_mgr = self._tracer.start_as_current_span(
                    f"rai.tool.{tool_name}",
                    context=parent_ctx,  # explicit parent = active node span
                )
                span = ctx_mgr.__enter__()
                span.set_attribute("openinference.span.kind", "TOOL")
                span.set_attribute("tool.name", tool_name)
                # Truncate large inputs so Phoenix doesn't reject the span
                span.set_attribute("input.value", input_str[:2000])
                if self._session_id:
                    span.set_attribute("session.id", self._session_id)
                self._tool_span_ctxs[run_id] = ctx_mgr
            except Exception as _exc:
                logger.debug("GovernedGraph: tool span open failed: %s", _exc)

        if self._tool_filter is not None and tool_name not in self._tool_filter:
            return  # Not in filter list — skip governance evaluation

        # ── A2A via tool trigger (guaranteed across all LangGraph versions) ──
        # When a tool is declared in a2a_tools, the act of calling it
        # represents an inter-agent handoff.  Evaluate the tool's input
        # (which contains the agent's working context — e.g. wire details)
        # as AGENT_TO_AGENT *before* the normal TOOL_CALL evaluation.
        if self._a2a_tools and tool_name in self._a2a_tools:
            a2a_agent = self._a2a_tools[tool_name]
            # Capture the CALLING agent before potentially overwriting _current_node.
            # This becomes source_agent in the A2A audit metadata so the interaction
            # graph can derive a meaningful edge (source ≠ target).
            _src_agent = self._current_node  # may be None if on_chain_start never fired
            # A graph may DECLARE the handoff source explicitly by passing
            # `from_agent` in the tool input. on_chain_start does not reliably set
            # _current_node across LangGraph versions, so an explicit from_agent is
            # authoritative for the interaction graph — it lets a front-line agent
            # (e.g. "banking_agent") appear as the source of its handoff to the
            # coordinator instead of being dropped (empty source → no edge).
            _declared_src = self._declared_from_agent(input_str)
            if _declared_src:
                _src_agent = _declared_src
            if self._current_node is None:
                self._current_node = a2a_agent

            # ── Token attribution resolution ──────────────────────────────
            # LangGraph fires on_chain_start with the Python function name
            # (e.g. "intl_wire_agent_node") rather than the label we used in
            # a2a_tools ("intl_wire_agent"), or may not fire it at all.
            # Try all three possible keys the pre-tool LLM calls may be under.
            a2a_tok = (
                self._token_usage.get(a2a_agent)           # exact match
                or self._token_usage.get(f"{a2a_agent}_node")  # _node suffix variant
                or self._token_usage.get("_unknown")        # on_chain_start didn't fire
                or {}
            )
            logger.info(
                "GovernedGraph: Layer A2A (tool-trigger) — evaluating "
                "AGENT_TO_AGENT for tool '%s' → agent '%s' tokens=%s",
                tool_name, a2a_agent, a2a_tok.get("total_tokens", 0),
            )
            a2a_content = self._format_a2a_tool_content(tool_name, input_str, a2a_agent)
            try:
                a2a_result = self._client.evaluate(
                    content=a2a_content,
                    role=ContentRole.AGENT_TO_AGENT,
                    request_id=str(uuid.uuid4()),
                    session_id=self._session_id,
                    eval_mode=self._eval_mode,
                    raise_on_block=False,
                    metadata={
                        "agent_name":    a2a_agent,
                        "source_agent":  _src_agent or "",   # calling agent (for interaction graph)
                        "tool_name":     tool_name,
                        "agent_tokens":  a2a_tok,
                    },
                )
                if self._on_result:
                    try:
                        self._on_result(a2a_result, ContentRole.AGENT_TO_AGENT)
                    except Exception:
                        pass
                if (a2a_result.verdict == Verdict.COERCIVE
                        and self._enforcement_mode != EnforcementMode.NONE):
                    logger.warning(
                        "GovernedGraph: A2A COERCIVE (tool-trigger) — blocking "
                        "tool '%s' violations=%s", tool_name, a2a_result.violations,
                    )
                    # Close tool span before blocking so it's properly exported
                    _close_span_ctx(self._tool_span_ctxs.pop(run_id, None))
                    raise GovernanceBlock(a2a_result)

                # ── HITL: directive-based check (primary path) ────────────
                # The PE signals REQUIRE_HUMAN_REVIEW via a Directive in the
                # response.  Check directives first — they carry full context
                # (category, options, source) needed for the HITL UI.
                # agent=a2a_agent / tool=tool_name propagate into the payload
                # so server-side handlers can identify the originating agent
                # (e.g. distinguish card_advisor A2A from transfer_agent HITL).
                _raise_if_hitl_directive(
                    a2a_result,
                    self._session_id,
                    run_id=run_id,
                    span_ctxs=self._tool_span_ctxs,
                    agent=a2a_agent,
                    tool=tool_name,
                )

                # ── HITL: deprecated counter fallback ────────────────────
                # Legacy mechanism kept for backward compatibility when
                # hitl_on_normative_a2a=True is explicitly set and no PE
                # directive was present.  Will be removed in a future release.
                if (a2a_result.verdict != Verdict.COERCIVE
                        and self._hitl_on_normative_a2a):
                    sid = self._session_id or ""
                    if sid not in self._hitl_cleared_sessions:
                        self._hitl_normative_counts[sid] = (
                            self._hitl_normative_counts.get(sid, 0) + 1
                        )
                        count = self._hitl_normative_counts[sid]
                        logger.info(
                            "GovernedGraph: [DEPRECATED] counter-based NORMATIVE "
                            "A2A count=%d/%d session=%s tool=%s — "
                            "prefer PE REQUIRE_HUMAN_REVIEW directive instead",
                            count, self._hitl_normative_threshold, sid, tool_name,
                        )
                        if count >= self._hitl_normative_threshold:
                            _close_span_ctx(self._tool_span_ctxs.pop(run_id, None))
                            raise HITLInterrupt({
                                "reason":          "governance_review_required",
                                "category":        None,
                                "options":         [],
                                "source":          "sdk_counter",
                                "violations":      a2a_result.violations or [],
                                "agent":           a2a_agent,
                                "tool":            tool_name,
                                "session_id":      sid,
                                "normative_count": count,
                            })
            except (GovernanceBlock, HITLInterrupt):
                raise  # Propagate both block and HITL interrupt to the graph caller
            except Exception as exc:
                logger.warning(
                    "GovernedGraph: A2A PE error (tool-trigger) for '%s': %s",
                    tool_name, exc,
                )

        # NONE enforcement: evaluate for observability but never block
        if self._enforcement_mode == EnforcementMode.NONE:
            self._evaluate_and_notify(tool_name, input_str, run_id)
            return

        content = f"Tool: {tool_name}\nInput: {input_str}"
        logger.debug("GovernedGraph: Layer 2 — evaluating TOOL_CALL for '%s'", tool_name)

        try:
            node = self._current_node or ""
            result = self._client.evaluate(
                content=content,
                role=ContentRole.TOOL_CALL,
                request_id=str(run_id),
                session_id=self._session_id,
                eval_mode=self._eval_mode,
                raise_on_block=False,
                metadata={
                    "tool_name":    tool_name,
                    "agent_name":   self._current_node,
                    "agent_tokens": (
                        self._token_usage.get(node)
                        or self._token_usage.get("_unknown")
                        or {}
                    ),
                },
            )
            if self._on_result:
                try:
                    self._on_result(result, ContentRole.TOOL_CALL)
                except Exception:
                    pass

            if result.verdict == Verdict.COERCIVE:
                # Both REDACT and RAISE modes must block the tool call.
                # GovernedGraph.invoke() catches this and applies the mode-specific
                # response (substitute message for REDACT, re-raise for RAISE).
                logger.warning(
                    "GovernedGraph: Layer 2 COERCIVE — blocking tool '%s' violations=%s",
                    tool_name, result.violations,
                )
                # Close tool span before blocking so it's properly exported
                _close_span_ctx(self._tool_span_ctxs.pop(run_id, None))
                raise GovernanceBlock(result)

            # Check for HITL directives (e.g. SANCTIONS_REVIEW on tool execution)
            _raise_if_hitl_directive(
                result,
                self._session_id,
                run_id=run_id,
                span_ctxs=self._tool_span_ctxs,
            )

        except (GovernanceBlock, HITLInterrupt):
            raise  # Propagate to GovernedGraph.invoke()
        except Exception as exc:
            logger.warning(
                "GovernedGraph: Layer 2 PE error — failing open for tool '%s': %s",
                tool_name, exc,
            )

    def _evaluate_and_notify(self, tool_name: str, input_str: str, run_id: UUID) -> None:
        """NONE mode: evaluate for audit trail only, never block."""
        try:
            content = f"Tool: {tool_name}\nInput: {input_str}"
            result = self._client.evaluate(
                content=content,
                role=ContentRole.TOOL_CALL,
                request_id=str(run_id),
                session_id=self._session_id,
                eval_mode=self._eval_mode,
                raise_on_block=False,
                metadata={
                    "tool_name":    tool_name,
                    "agent_name":   self._current_node,
                    "agent_tokens": (
                        self._token_usage.get(self._current_node or "")
                        or self._token_usage.get("_unknown")
                        or {}
                    ),
                },
            )
            if self._on_result:
                try:
                    self._on_result(result, ContentRole.TOOL_CALL)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("GovernedGraph: Layer 2 (NONE) PE error: %s", exc)

    # ── Agent name discovery ──────────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Any = None,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fires when any LangGraph node (or LangChain chain) begins execution.

        We update _current_node only for snake_case node names — this matches
        user-defined LangGraph nodes (router, intl_wire_agent, etc.) and
        excludes LangChain internals (ChatGroq, RunnableSequence, etc.).

        When a node is in _agent_nodes, its working context (state messages +
        routing metadata) is evaluated as AGENT_TO_AGENT before it executes.
        """
        # LangGraph fires on_chain_start with serialized=None for some internal
        # chain events (e.g. RunnableLambda wrappers with no metadata).  Guard
        # before .get() to avoid AttributeError propagating through LangChain's
        # CallbackManager as a WARNING and leaving _current_node unset.
        if not serialized:
            return
        node_name = serialized.get("name", "")
        if node_name and _NODE_NAME_RE.match(node_name):
            self._current_node = node_name
            logger.info(
                "GovernedGraph: node start — '%s' (a2a_watch=%s)",
                node_name,
                node_name in self._agent_nodes if self._agent_nodes else False,
            )

            # ── OTel node span ────────────────────────────────────────────
            # Opens an AGENT span as a child of the active root
            # (rai.governance.turn).  GUARDRAIL child spans emitted by
            # TrustClient.evaluate() inside this node will automatically nest
            # under it in Phoenix/Arize because start_as_current_span() sets
            # this span as the active context.
            if self._tracer:
                try:
                    ctx_mgr = self._tracer.start_as_current_span(
                        f"rai.agent.{node_name}"
                    )
                    span = ctx_mgr.__enter__()
                    span.set_attribute("openinference.span.kind", "AGENT")
                    span.set_attribute("rai.agent.id",   node_name)
                    span.set_attribute("rai.app_id",     self._app_id)
                    if self._session_id:
                        span.set_attribute("session.id", self._session_id)
                    self._node_span_ctxs[run_id] = ctx_mgr
                except Exception as _exc:
                    logger.debug("GovernedGraph: node span open failed: %s", _exc)

            # ── A2A evaluation ────────────────────────────────────────────
            if self._agent_nodes and node_name in self._agent_nodes and inputs:
                content = self._extract_a2a_content(inputs, node_name)
                if content:
                    logger.info(
                        "GovernedGraph: Layer A2A — evaluating AGENT_TO_AGENT "
                        "for node '%s'", node_name
                    )
                    try:
                        result = self._client.evaluate(
                            content=content,
                            role=ContentRole.AGENT_TO_AGENT,
                            request_id=str(run_id),
                            session_id=self._session_id,
                            eval_mode=self._eval_mode,
                            raise_on_block=False,
                            metadata={
                                "agent_name":   node_name,
                                "agent_tokens": self._token_usage.get(node_name, {}),
                            },
                        )
                        if self._on_result:
                            try:
                                self._on_result(result, ContentRole.AGENT_TO_AGENT)
                            except Exception:
                                pass

                        if (result.verdict == Verdict.COERCIVE
                                and self._enforcement_mode != EnforcementMode.NONE):
                            logger.warning(
                                "GovernedGraph: A2A COERCIVE — blocking node '%s' "
                                "violations=%s", node_name, result.violations,
                            )
                            raise GovernanceBlock(result)

                        # Check for HITL directives (e.g. AGENT_ARBITRATION on chain A2A)
                        _raise_if_hitl_directive(result, self._session_id)

                    except (GovernanceBlock, HITLInterrupt):
                        raise
                    except Exception as exc:
                        logger.warning(
                            "GovernedGraph: A2A PE error for node '%s' — "
                            "failing open: %s", node_name, exc,
                        )

    def _extract_a2a_content(self, inputs: Any, node_name: str) -> str:
        """
        Build an evaluable content string from the agent's working context
        (LangGraph state dict passed to the node on entry).
        """
        if not isinstance(inputs, dict):
            try:
                inputs = dict(inputs)
            except Exception:
                return ""

        parts = [f"Agent: {node_name}"]

        # Routing metadata (intent, transfer_type, etc.)
        for key in ("intent", "transfer_type"):
            val = inputs.get(key)
            if val:
                parts.append(f"{key}: {val}")

        # Last 5 messages for context
        messages = inputs.get("messages", [])
        for msg in messages[-5:]:
            role    = getattr(msg, "type", None) or getattr(msg, "role", None)
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content.strip():
                if role == "human":
                    parts.append(f"User: {content.strip()}")
                elif role == "ai":
                    # Truncate long AI messages
                    text = content.strip()
                    parts.append(f"Prior agent: {text[:300]}{'…' if len(text) > 300 else ''}")

        return "\n".join(parts)

    @staticmethod
    def _declared_from_agent(input_str: str) -> Optional[str]:
        """
        Return an explicitly-declared handoff source (``from_agent``) from a
        tool's input, or None. Lets a graph author make the interaction graph
        reflect the real handoff direction (front-line agent → coordinator)
        instead of relying on version-dependent on_chain_start node attribution.
        Only honoured when the tool input declares it — all other A2A tools are
        unaffected.

        The tool input arrives either as JSON (double-quoted) or as a Python dict
        repr (single-quoted, e.g. ``{'from_agent': 'banking_agent'}``) depending on
        the LangChain version, so try JSON first then ``ast.literal_eval``.
        """
        if not input_str:
            return None
        parsed = None
        try:
            import json
            parsed = json.loads(input_str)
        except Exception:
            try:
                import ast
                parsed = ast.literal_eval(input_str)
            except Exception:
                return None
        if isinstance(parsed, dict):
            v = parsed.get("from_agent")
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _format_a2a_tool_content(
        self, tool_name: str, input_str: str, agent_name: str
    ) -> str:
        """
        Format the A2A evaluation content from a tool's input.

        The tool input (e.g. international_transfer args) IS the inter-agent
        working context — it contains all the sensitive data (beneficiary,
        SWIFT code, country, amount) that sanctions / AML policies need to see.
        """
        parts = [f"Agent: {agent_name}", f"Invoked tool: {tool_name}"]
        # Try to parse the input_str as JSON for cleaner formatting
        try:
            import json
            parsed = json.loads(input_str)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    parts.append(f"{k}: {v}")
            else:
                parts.append(f"Input: {input_str}")
        except Exception:
            parts.append(f"Input: {input_str}")
        return "\n".join(parts)

    # ── Token usage + cost capture ────────────────────────────────────────

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fires when a ChatModel (ChatGroq, ChatOpenAI, etc.) begins inference.

        We capture the model name here as a fallback, in case llm_output does
        not include it in on_llm_end (can happen with certain LangChain versions
        or when using bind_tools wrappers).

        In LangGraph 1.x, on_chain_start does not fire for individual nodes.
        LangGraph passes the node name in kwargs['metadata']['langgraph_node']
        which we use to update _current_node and emit node spans here instead.
        """
        # ── LangGraph 1.x node attribution ────────────────────────────────────
        # In LangGraph >= 1.0, individual node execution does not fire
        # on_chain_start.  The node name is available in the 'metadata' kwarg
        # as metadata['langgraph_node'].  We use this to set _current_node and,
        # when the tracer is active, open an AGENT span for the node.
        metadata = kwargs.get("metadata") or {}
        lg_node = metadata.get("langgraph_node", "")
        if lg_node and _NODE_NAME_RE.match(lg_node):
            if self._current_node != lg_node:
                logger.info(
                    "GovernedGraph: node (via LLM) start — '%s'", lg_node,
                )
                # ── OTel node span lifecycle (LangGraph 1.x) ─────────────────
                # Node spans use tracer.start_span() — NOT start_as_current_span —
                # because LangGraph 1.x runs callbacks inside copy_context() and
                # start_as_current_span attaches a token that can only be detached
                # within the same context copy, causing
                #   "ValueError: Token was created in a different Context"
                # when we try to close spans from the main invocation context.
                #
                # Using start_span() + span.end() sidesteps context tokens entirely.
                # Tool spans still nest correctly because we pass node_span_ctx as
                # the explicit parent context when creating tool spans.
                #
                # Lifecycle:
                #   on_chat_model_start(nodeA) → close prev node span, open nodeA
                #   on_tool_start(tool)         → opens TOOL with nodeA as parent ✓
                #   on_tool_end(tool)           → closes tool span
                #   on_chat_model_start(nodeA)  → same node, no-op
                #   on_chat_model_start(nodeB)  → close nodeA, open nodeB
                #   _close_all_node_spans()     → called after graph.invoke() ends
                if self._tracer:
                    try:
                        from opentelemetry import trace as _ot_trace
                        from opentelemetry import context as _ot_ctx

                        # Close previous node span via span.end() (no context mgr)
                        prev = self._active_node_spans.pop(self._current_node, None) \
                            if self._current_node else None
                        if prev is not None:
                            _end_span_safe(prev[0])   # prev = (span, ctx)

                        # Create new node span as child of whatever is current
                        # in THIS copy context (root turn span, or parent node).
                        parent_ctx = _ot_ctx.get_current()
                        node_span = self._tracer.start_span(
                            f"rai.agent.{lg_node}",
                            context=parent_ctx,
                        )
                        node_span.set_attribute("openinference.span.kind", "AGENT")
                        node_span.set_attribute("rai.agent.id",   lg_node)
                        node_span.set_attribute("rai.app_id",     self._app_id)
                        if self._session_id:
                            node_span.set_attribute("session.id", self._session_id)

                        # Capture a context snapshot with this span as current
                        # so tool spans can use it as explicit parent.
                        node_span_ctx = _ot_trace.set_span_in_context(node_span)
                        self._active_node_spans[lg_node] = (node_span, node_span_ctx)
                    except Exception as _exc:
                        logger.debug("GovernedGraph: node span (LLM path) open failed: %s", _exc)
                self._current_node = lg_node
        model_name = (
            serialized.get("name")
            or serialized.get("kwargs", {}).get("model_name")
            or serialized.get("kwargs", {}).get("model")
            or ""
        )
        if model_name:
            self._current_model = model_name
            logger.debug("GovernedGraph: chat model start — model='%s' node='%s'", model_name, self._current_node)

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fires after every LLM call completes (ChatGroq, ChatOpenAI, etc.).

        Accumulates token counts and model name per node.  Results are carried
        in ``metadata.agent_tokens`` on subsequent PE evaluate() calls so the
        PAP Agent Activity table can compute cost and display per-agent totals.
        Cost computation is done platform-side (PAP) using litellm.

        Requires BaseCallbackHandler inheritance — without it, LangChain's
        CallbackManager silently drops this handler for LLM-lifecycle events
        even when the handler is correctly listed in config["callbacks"].
        """
        try:
            llm_output = getattr(response, "llm_output", None) or {}

            # ── Token extraction ─────────────────────────────────────────
            # Try standard locations in priority order.
            usage = (
                llm_output.get("token_usage")      # langchain_groq primary
                or llm_output.get("usage")          # OpenAI-compat fallback
                or {}
            )

            # Fallback: check generation_info on first generation (some versions)
            if not usage:
                try:
                    gens = getattr(response, "generations", [[]])
                    if gens and gens[0]:
                        gen_info = getattr(gens[0][0], "generation_info", {}) or {}
                        usage = (
                            gen_info.get("token_usage")
                            or gen_info.get("usage")
                            or {}
                        )
                        # OpenAI-style object with attributes instead of dict keys
                        if not isinstance(usage, dict) and hasattr(usage, "prompt_tokens"):
                            usage = {
                                "prompt_tokens":     getattr(usage, "prompt_tokens",     0),
                                "completion_tokens": getattr(usage, "completion_tokens", 0),
                                "total_tokens":      getattr(usage, "total_tokens",      0),
                            }
                except Exception:
                    pass

            # Handle CompletionUsage objects (pydantic models from openai SDK)
            if not isinstance(usage, dict) and hasattr(usage, "prompt_tokens"):
                usage = {
                    "prompt_tokens":     getattr(usage, "prompt_tokens",     0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens":      getattr(usage, "total_tokens",      0),
                }

            prompt_tokens     = int(usage.get("prompt_tokens",     0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            total_tokens      = int(usage.get("total_tokens", prompt_tokens + completion_tokens))

            if total_tokens == 0:
                logger.debug("GovernedGraph: on_llm_end — no token data (node=%s)", self._current_node)
                return

            node = self._current_node or "_unknown"
            entry = self._token_usage.setdefault(
                node,
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            entry["prompt_tokens"]     += prompt_tokens
            entry["completion_tokens"] += completion_tokens
            entry["total_tokens"]      += total_tokens

            # ── Model name (forwarded in metadata so PAP can compute cost) ─
            # Resolve model name: llm_output first, then on_chat_model_start capture
            model_name = (
                llm_output.get("model_name")
                or llm_output.get("model")
                or self._current_model
                or ""
            )
            if not model_name:
                try:
                    gens = getattr(response, "generations", [[]])
                    if gens and gens[0]:
                        gen_info = getattr(gens[0][0], "generation_info", {}) or {}
                        model_name = gen_info.get("model", "")
                except Exception:
                    pass

            if model_name:
                entry["model"] = model_name

            logger.info(
                "GovernedGraph: on_llm_end [%s] prompt=%d completion=%d total=%d model=%s",
                node, prompt_tokens, completion_tokens, total_tokens, model_name or "unknown",
            )
        except Exception as exc:
            logger.warning("GovernedGraph: token capture error: %s", exc)

    # ── Node span cleanup ─────────────────────────────────────────────────

    def _close_all_node_spans(self) -> None:
        """
        Close all open node spans at end of a governed turn.

        Node spans are kept open across LLM calls so that tool spans can nest
        inside them.  This method flushes whatever is left when graph.invoke()
        or graph.stream() finishes (normally or via GovernanceBlock).

        Uses _close_node_span() which falls back to span.end() if the OTel
        context manager's __exit__ fails across LangGraph copy_context() boundaries.
        """
        for _node_name, (span, _ctx) in list(self._active_node_spans.items()):
            _end_span_safe(span)
        self._active_node_spans.clear()

    # ── Span lifecycle: tool end / error ──────────────────────────────────

    def on_tool_end(
        self,
        output: Any = None,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the OTel tool span with the tool's output value."""
        ctx_mgr = self._tool_span_ctxs.pop(run_id, None)
        if ctx_mgr and output is not None:
            try:
                # Attach output before closing so Phoenix shows it in the span
                from opentelemetry import trace as _trace
                span = _trace.get_current_span()
                if span.is_recording():
                    out_str = str(output)
                    span.set_attribute("output.value", out_str[:2000])
            except Exception:
                pass
        _close_span_ctx(ctx_mgr)

    def on_tool_error(
        self,
        error: Any = None,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the OTel tool span, recording the error status."""
        ctx_mgr = self._tool_span_ctxs.pop(run_id, None)
        if ctx_mgr:
            try:
                from opentelemetry import trace as _trace
                from opentelemetry.trace import StatusCode
                span = _trace.get_current_span()
                if span.is_recording():
                    span.set_status(StatusCode.ERROR, str(error)[:500])
            except Exception:
                pass
        _close_span_ctx(ctx_mgr)

    # ── Span lifecycle: chain (node) end / error ──────────────────────────

    def on_chain_end(
        self,
        outputs: Any = None,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the OTel node span when a LangGraph node completes."""
        _close_span_ctx(self._node_span_ctxs.pop(run_id, None))

    def on_chain_error(
        self,
        error: Any = None,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the OTel node span with error status on node failure."""
        ctx_mgr = self._node_span_ctxs.pop(run_id, None)
        if ctx_mgr:
            try:
                from opentelemetry import trace as _trace
                from opentelemetry.trace import StatusCode
                span = _trace.get_current_span()
                if span.is_recording():
                    span.set_status(StatusCode.ERROR, str(error)[:500])
            except Exception:
                pass
        _close_span_ctx(ctx_mgr)

    # ── No-ops for remaining callback hooks ───────────────────────────────
    def on_llm_start(self, *a: Any, **kw: Any) -> None: pass
    def on_llm_error(self, *a: Any, **kw: Any) -> None: pass
    def on_agent_action(self, *a: Any, **kw: Any) -> None: pass
    def on_agent_finish(self, *a: Any, **kw: Any) -> None: pass


# ── Directive enforcement helper ──────────────────────────────────────────────

def _raise_if_hitl_directive(
    result:     GovernanceResult,
    session_id: Optional[str],
    run_id:     Any = None,
    span_ctxs:  Optional[Dict] = None,
    agent:      Optional[str] = None,
    tool:       Optional[str] = None,
) -> None:
    """
    Inspect ``result.directives`` for a REQUIRE_HUMAN_REVIEW directive and,
    if found, raise ``HITLInterrupt`` with the full directive payload.

    Called after every PE ``evaluate()`` response so that governance pauses
    requested by the Policy Engine (or routed from Sentinel via the PE) are
    honoured at every evaluation point — user prompt, A2A, tool call, and
    LLM response layers.

    Parameters
    ──────────
    result      : GovernanceResult from the PE evaluation call.
    session_id  : Active session ID — included in the interrupt payload.
    run_id      : LangChain run_id; when provided the matching entry is popped
                  from ``span_ctxs`` and closed before raising, so OTel traces
                  are not left in a dangling open state.
    span_ctxs   : Callback span dict (e.g. ``_tool_span_ctxs``) to close the
                  ``run_id`` entry from before raising.
    agent       : Name of the agent that triggered this evaluation (e.g. the
                  A2A target agent or the node currently executing).  Included
                  in the HITLInterrupt payload so server-side handlers can
                  identify which agent caused the interrupt without parsing
                  directive metadata.
    tool        : Name of the tool whose invocation triggered the evaluation.
                  Included in the payload for the same reason as ``agent``.
    """
    if not result.directives:
        return
    for directive in sorted(
        result.directives,
        key=lambda d: _DIRECTIVE_PRIORITY.get(d.action, 0),
        reverse=True,
    ):
        if directive.action == DirectiveAction.REQUIRE_HUMAN_REVIEW:
            if run_id is not None and span_ctxs is not None:
                _close_span_ctx(span_ctxs.pop(run_id, None))
            raise HITLInterrupt({
                "reason":     directive.reason or "governance_review_required",
                "category":   directive.category.value if directive.category else None,
                "options":    directive.options,
                "source":     directive.source.value,
                "session_id": session_id or "",
                "directive":  directive.model_dump(),
                "agent":      agent or "",
                "tool":       tool  or "",
            })


# ── OTel span helpers ────────────────────────────────────────────────────────

def _close_span_ctx(ctx_mgr: Any) -> None:
    """
    Safely close an OTel context manager returned by
    ``tracer.start_as_current_span()``.  Never raises — span cleanup must
    never propagate errors into application code.
    """
    if ctx_mgr is None:
        return
    try:
        ctx_mgr.__exit__(None, None, None)
    except Exception as _exc:
        logger.debug("GovernedGraph: span close error (ignored): %s", _exc)


def _end_span_safe(span: Any) -> None:
    """
    End an OTel span by calling ``span.end()`` directly.

    Used for node spans that were created with ``tracer.start_span()`` (not
    ``start_as_current_span``) to avoid OTel context-token cross-boundary
    errors in LangGraph 1.x, where callbacks run inside ``copy_context().run()``.
    """
    if span is None:
        return
    try:
        span.end()
    except Exception as _exc:
        logger.debug("GovernedGraph: span.end() error (ignored): %s", _exc)


# ── State mutation helpers ────────────────────────────────────────────────────

def _inject_ai_message(state: Any, text: str) -> dict:
    """
    Return a copy of *state* (or any dict) with a new AIMessage appended.
    Used when the graph was not invoked (Layer 1 block) so we need to
    synthesise a full output dict.
    """
    try:
        from langchain_core.messages import AIMessage
        ai_msg: Any = AIMessage(content=text)
    except ImportError:
        ai_msg = {"role": "assistant", "content": text}

    base = dict(state) if isinstance(state, dict) else {}
    existing = list(_get_messages(state))
    return {**base, "messages": existing + [ai_msg]}


def _replace_last_human(state: Any, text: str) -> Any:
    """Return a copy of state with the last HumanMessage content replaced."""
    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        return state
    if not isinstance(state, dict):
        return state
    messages = list(state.get("messages", []))
    for i in reversed(range(len(messages))):
        if getattr(messages[i], "type", None) == "human":
            messages[i] = HumanMessage(content=text)
            break
    return {**state, "messages": messages}


def _replace_last_ai(output: Any, text: str) -> Any:
    """
    Return a copy of *output* with the last AIMessage replaced by *text*.
    Used for Layer 3 redaction — graph has already run, output exists.
    """
    try:
        from langchain_core.messages import AIMessage
    except ImportError:
        return output  # Can't manipulate messages without langchain-core

    if not isinstance(output, dict):
        return output

    messages = list(output.get("messages", []))
    for i in reversed(range(len(messages))):
        if getattr(messages[i], "type", None) == "ai":
            messages[i] = AIMessage(content=text)
            break

    return {**output, "messages": messages}


# ── Content extraction helpers ────────────────────────────────────────────────

def _extract_last_human(state: Any) -> str:
    """Pull the most recent human message text from graph state."""
    messages = _get_messages(state)
    for msg in reversed(messages):
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if role == "human":
            return _msg_content(msg)
    return ""


def _extract_last_ai(state: Any) -> str:
    """Pull the most recent AI message text from graph state."""
    messages = _get_messages(state)
    for msg in reversed(messages):
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if role == "ai":
            content = _msg_content(msg)
            # Skip tool-call-only AI messages (no text content)
            if content:
                return content
    return ""


def _get_messages(state: Any) -> list:
    if isinstance(state, dict):
        return state.get("messages", [])
    return getattr(state, "messages", [])


def _msg_content(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def _extract_session_id(state: Any) -> Optional[str]:
    if isinstance(state, dict):
        return state.get("session_id")
    return getattr(state, "session_id", None)


def _extract_customer_id(state: Any) -> Optional[str]:
    """Extract customer/user ID from graph state for Arize ``user.id`` attribution."""
    if isinstance(state, dict):
        return state.get("customer_id")
    return getattr(state, "customer_id", None)


def _record_token_usage(tool_cb: "_ToolGuardCallback") -> None:
    """
    Write accumulated LLM token counts onto the currently-active OTel span.

    Called after ``graph.invoke()`` / ``graph.stream()`` completes so that the
    root ``rai.governance.turn`` span carries Arize Tier 2 cost attributes:

      llm.token_count.prompt      — total prompt tokens across all nodes
      llm.token_count.completion  — total completion tokens
      llm.token_count.total       — grand total
      llm.model_name              — model used (first observed, e.g. "llama3-70b")
    """
    if not tool_cb._token_usage:
        return
    try:
        from opentelemetry import trace as _trace
        from openinference.semconv.trace import SpanAttributes as _SA
        span = _trace.get_current_span()
        if not span.is_recording():
            return
        prompt     = sum(t.get("prompt_tokens",     0) for t in tool_cb._token_usage.values())
        completion = sum(t.get("completion_tokens", 0) for t in tool_cb._token_usage.values())
        total      = sum(t.get("total_tokens",      0) for t in tool_cb._token_usage.values())
        if total:
            span.set_attribute(_SA.LLM_TOKEN_COUNT_PROMPT,     prompt)
            span.set_attribute(_SA.LLM_TOKEN_COUNT_COMPLETION, completion)
            span.set_attribute(_SA.LLM_TOKEN_COUNT_TOTAL,      total)
        models = {t["model"] for t in tool_cb._token_usage.values() if t.get("model")}
        if models:
            span.set_attribute(_SA.LLM_MODEL_NAME, next(iter(models)))
    except ImportError:
        # openinference not installed — fall back to raw string literals (same values)
        try:
            from opentelemetry import trace as _trace2
            span = _trace2.get_current_span()
            if span.is_recording():
                prompt     = sum(t.get("prompt_tokens",     0) for t in tool_cb._token_usage.values())
                completion = sum(t.get("completion_tokens", 0) for t in tool_cb._token_usage.values())
                total      = sum(t.get("total_tokens",      0) for t in tool_cb._token_usage.values())
                if total:
                    span.set_attribute("llm.token_count.prompt",     prompt)
                    span.set_attribute("llm.token_count.completion", completion)
                    span.set_attribute("llm.token_count.total",      total)
                models = {t["model"] for t in tool_cb._token_usage.values() if t.get("model")}
                if models:
                    span.set_attribute("llm.model_name", next(iter(models)))
        except Exception:
            pass
    except Exception:
        pass  # Never let OTel errors surface to the application
