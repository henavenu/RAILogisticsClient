"""
LangGraphGuard — explicit node-level governance for LangGraph.

Unlike RaiTrustCallbackHandler (which hooks into LangChain events implicitly),
LangGraphGuard is applied declaratively to individual node functions.  This
gives precise control over exactly which nodes are governed, at which content
roles, and using which content extractor.

Why a separate guard for LangGraph?
────────────────────────────────────
LangGraph's StateGraph has no ``Agent`` class — the graph IS the agent.  Nodes
are plain Python functions (or coroutines).  The callback system works for
observability but enforcement (raising mid-graph) is cleaner when done
explicitly in the node wrapper, because:

  1. LangGraph can checkpoint and retry individual nodes.  An exception raised
     in a callback fires *after* the node has already run; a wrapper raises
     *before* or *after* the node function depending on the phase.
  2. You want governance to be visible in the graph definition, not hidden in a
     global callback registration.

Usage
─────
::

    from rai_trust_sdk import TrustClient
    from rai_trust_sdk.langgraph import LangGraphGuard
    from rai_trust_sdk.types import ContentRole

    client = TrustClient(base_url="http://pe:3000", app_id="finapp")
    guard  = LangGraphGuard(client=client)

    # Decorate a node — evaluates input before node runs, output after
    @guard.wrap(
        input_role=ContentRole.USER_PROMPT,
        output_role=ContentRole.LLM_RESPONSE,
        content_key="messages",
    )
    def llm_node(state: AgentState) -> AgentState:
        response = llm.invoke(state["messages"])
        return {"messages": state["messages"] + [response]}

    # Add to graph as normal
    graph.add_node("llm", llm_node)

    # Custom extractor — extract text from a specific field
    @guard.wrap(
        input_role=ContentRole.USER_PROMPT,
        extract_input=lambda s: s["user_input"],
        output_role=None,   # skip post-node check
    )
    def input_validator(state):
        ...
"""
from __future__ import annotations

import functools
import logging
import uuid
from typing import Any, Callable, Optional, TypeVar

from ..client import TrustClient
from ..types import ContentRole, EvalMode, GovernanceBlock, GovernanceResult

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class LangGraphGuard:
    """
    Governance decorator factory for LangGraph node functions.

    Parameters
    ──────────
    client : TrustClient
        Configured client for the target app.
    session_id : str, optional
        Multi-turn session ID passed to every evaluate call made by this guard.
    eval_mode : EvalMode
        ``SYNC`` (default): raises ``GovernanceBlock`` on COERCIVE verdict.
        ``ASYNC``: fire-and-log, never blocks.
    on_result : callable, optional
        ``on_result(result: GovernanceResult, role: ContentRole)`` — called
        after every successful evaluation for logging / observability.
    """

    def __init__(
        self,
        client: TrustClient,
        session_id: Optional[str] = None,
        eval_mode: EvalMode = EvalMode.SYNC,
        on_result: Optional[Callable[[GovernanceResult, ContentRole], None]] = None,
    ) -> None:
        self._client     = client
        self._session_id = session_id
        self._eval_mode  = eval_mode
        self._on_result  = on_result

    # ── Decorator factory ──────────────────────────────────────────────────────

    def wrap(
        self,
        *,
        input_role:      Optional[ContentRole] = ContentRole.USER_PROMPT,
        output_role:     Optional[ContentRole] = ContentRole.LLM_RESPONSE,
        content_key:     str = "messages",
        extract_input:   Optional[Callable[[Any], str]] = None,
        extract_output:  Optional[Callable[[Any], str]] = None,
    ) -> Callable[[F], F]:
        """
        Return a decorator that wraps a LangGraph node function with governance.

        Parameters
        ──────────
        input_role : ContentRole | None
            Role for the pre-node evaluation.  ``None`` skips the pre-check.
        output_role : ContentRole | None
            Role for the post-node evaluation.  ``None`` skips the post-check.
        content_key : str
            State key used to extract content when no extractor is supplied.
            Default ``"messages"`` (the standard LangGraph message list key).
        extract_input : callable, optional
            ``(state) -> str`` — custom extractor for the pre-node content.
            Falls back to ``content_key`` lookup when not provided.
        extract_output : callable, optional
            ``(output_state) -> str`` — custom extractor for the post-node
            content.  Falls back to ``content_key`` lookup when not provided.

        Returns
        ───────
        A decorator that wraps the node function.
        """
        guard = self

        def decorator(fn: F) -> F:
            @functools.wraps(fn)
            def wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
                run_id = str(uuid.uuid4())

                # ── Pre-node: evaluate input ───────────────────────────────
                if input_role is not None:
                    content = _extract_content(state, content_key, extract_input)
                    if content:
                        guard._evaluate(content, input_role, run_id, phase="pre")

                # ── Execute the node ───────────────────────────────────────
                output = fn(state, *args, **kwargs)

                # ── Post-node: evaluate output ─────────────────────────────
                if output_role is not None:
                    content = _extract_content(output, content_key, extract_output)
                    if content:
                        guard._evaluate(content, output_role, run_id, phase="post")

                return output

            return wrapper  # type: ignore[return-value]

        return decorator

    # ── Internal ───────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        content: str,
        role: ContentRole,
        run_id: str,
        phase: str,
    ) -> Optional[GovernanceResult]:
        try:
            result = self._client.evaluate(
                content=content,
                role=role,
                request_id=f"{run_id}-{phase}",
                session_id=self._session_id,
                eval_mode=self._eval_mode,
                metadata={"phase": phase, "guard": "LangGraphGuard"},
                raise_on_block=True,
            )
            if self._on_result:
                try:
                    self._on_result(result, role)
                except Exception:
                    pass
            return result
        except GovernanceBlock:
            raise  # Propagates out of the node → LangGraph catches as graph error
        except Exception as exc:
            logger.warning(
                f"LangGraphGuard: evaluation error "
                f"phase={phase} role={role.value} run_id={run_id}: {exc}"
            )
            return None


# ── Content extraction helpers ─────────────────────────────────────────────────

def _extract_content(
    state: Any,
    content_key: str,
    extractor: Optional[Callable[[Any], str]],
) -> str:
    """
    Extract a plain-text string from a LangGraph state object.

    Resolution order:
      1. Custom ``extractor`` callable (if provided)
      2. ``state[content_key]`` dict access
      3. ``state.<content_key>`` attribute access
      4. ``str(state)`` fallback
    """
    if extractor is not None:
        return (extractor(state) or "").strip()

    # Dict-style (TypedDict, plain dict)
    if isinstance(state, dict):
        value = state.get(content_key)
    else:
        value = getattr(state, content_key, None)

    if value is None:
        return ""

    return _coerce_to_str(value)


def _coerce_to_str(value: Any) -> str:
    """Coerce a state value to plain text."""
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            else:
                # LangChain BaseMessage-compatible objects
                content = getattr(item, "content", None)
                if content is None:
                    continue
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p.strip())

    return str(value).strip()
