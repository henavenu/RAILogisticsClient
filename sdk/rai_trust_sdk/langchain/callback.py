"""
RaiTrustCallbackHandler — LangChain callback integration for the RAI Trust SDK.

Option B: Role filter with outer-edge default
─────────────────────────────────────────────
The handler hooks into LangChain's callback system and evaluates content at
configurable role boundaries.  Default behaviour:

  • USER_PROMPT  → evaluated in ``on_chat_model_start`` / ``on_llm_start``
                   when the call is the outermost LLM invocation
                   (``parent_run_id is None``)
  • LLM_RESPONSE → evaluated in ``on_llm_end`` at the same outer-edge boundary

Inner LLM calls (nested inside tool calls, sub-chains, agents) are skipped by
default, keeping governance focused on the application boundary — what went in
and what came out — rather than every intermediate inference step.

SYNC enforcement
────────────────
When ``eval_mode=SYNC`` (the default) and the Policy Engine returns COERCIVE,
``GovernanceBlock`` is raised inside the callback.  LangChain propagates
exceptions from callbacks back to the ``chain.invoke()`` caller, so the
application receives the block without any additional wiring.

Usage
─────
::

    from rai_trust_sdk import TrustClient
    from rai_trust_sdk.langchain import RaiTrustCallbackHandler

    client = TrustClient(base_url="http://pe:3000", app_id="finapp")
    handler = RaiTrustCallbackHandler(client=client)

    # Attach per-invocation (preferred — no global state)
    result = chain.invoke(
        {"input": "..."},
        config={"callbacks": [handler]},
    )

    # Or attach at chain construction time
    chain = prompt | llm.with_config(callbacks=[handler])
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID

from ..client import TrustClient
from ..types import ContentRole, EvalMode, GovernanceBlock, GovernanceResult

logger = logging.getLogger(__name__)


class RaiTrustCallbackHandler:
    """
    LangChain ``BaseCallbackHandler`` subclass that routes content to the
    RAI Trust Platform for governance evaluation.

    Parameters
    ──────────
    client : TrustClient
        Configured client for the target app.
    roles : set[ContentRole], optional
        Which content roles to evaluate.
        Default: ``{USER_PROMPT, LLM_RESPONSE}``.
    outer_only : bool
        When ``True`` (default), only evaluate when ``parent_run_id is None``
        — the outermost chain/LLM invocation, not nested tool or sub-chain
        calls.
    session_id : str, optional
        Multi-turn session ID.  Enables cumulative scoring across turns.
    eval_mode : EvalMode
        ``SYNC`` (default): raises ``GovernanceBlock`` on COERCIVE verdict.
        ``ASYNC``: fire-and-log, never blocks.
    on_result : callable, optional
        ``on_result(result: GovernanceResult, role: ContentRole)`` — called
        after every successful evaluation for logging / metrics.
    raise_errors : bool
        If ``True``, re-raise transport / serialisation errors.
        Default ``False`` — log warning and continue.
    """

    # Declare as callback handler without hard import of langchain-core at module
    # load time so the SDK core can be imported even without [langchain] extra.
    # The actual BaseCallbackHandler mixin is injected lazily via __init_subclass__
    # or simply by duck-typing (LangChain accepts any object with on_* methods).

    def __init__(
        self,
        client: TrustClient,
        roles: Optional[Set[ContentRole]] = None,
        outer_only: bool = True,
        session_id: Optional[str] = None,
        eval_mode: EvalMode = EvalMode.SYNC,
        on_result: Optional[Callable[[GovernanceResult, ContentRole], None]] = None,
        raise_errors: bool = False,
    ) -> None:
        # Lazy import — only required when [langchain] extra is installed
        try:
            from langchain_core.callbacks.base import BaseCallbackHandler
            # Patch self's class to inherit BaseCallbackHandler so LangChain
            # recognises it properly.  We do this once per process.
            if not isinstance(self, BaseCallbackHandler):
                self.__class__ = type(
                    "RaiTrustCallbackHandler",
                    (RaiTrustCallbackHandler, BaseCallbackHandler),
                    {},
                )
                BaseCallbackHandler.__init__(self)
        except ImportError:
            raise ImportError(
                "langchain-core is required for RaiTrustCallbackHandler. "
                "Install with: pip install 'rai-trust-sdk[langchain]'"
            )

        self._client     = client
        self._roles      = roles or {ContentRole.USER_PROMPT, ContentRole.LLM_RESPONSE}
        self._outer_only = outer_only
        self._session_id = session_id
        self._eval_mode  = eval_mode
        self._on_result  = on_result
        self._raise_errors = raise_errors

    # ── Private helpers ────────────────────────────────────────────────────────

    def _is_outer(self, parent_run_id: Optional[UUID]) -> bool:
        """True when this invocation is the outermost (no parent) or outer_only=False."""
        return (not self._outer_only) or (parent_run_id is None)

    @staticmethod
    def _extract_from_messages(messages: List[List[Any]]) -> str:
        """Flatten LangChain message batches to plain text."""
        parts: List[str] = []
        for batch in messages:
            for msg in batch:
                content = getattr(msg, "content", None)
                if content is None:
                    continue
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    # Multi-modal content blocks — extract text only
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p.strip())

    @staticmethod
    def _extract_from_result(response: Any) -> str:
        """Extract generated text from an LLMResult."""
        parts: List[str] = []
        for gen_list in getattr(response, "generations", []):
            for gen in gen_list:
                # ChatGeneration
                if hasattr(gen, "message"):
                    content = getattr(gen.message, "content", "")
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                # Standard Generation
                elif hasattr(gen, "text"):
                    parts.append(gen.text or "")
        return "\n".join(p for p in parts if p.strip())

    def _evaluate(
        self,
        content: str,
        role: ContentRole,
        run_id: UUID,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[GovernanceResult]:
        """Run governance evaluation; raise GovernanceBlock or log errors."""
        if not content.strip():
            return None
        if role not in self._roles:
            return None

        try:
            result = self._client.evaluate(
                content=content,
                role=role,
                request_id=str(run_id),
                session_id=self._session_id,
                eval_mode=self._eval_mode,
                metadata=metadata,
                raise_on_block=True,  # GovernanceBlock propagates up through LangChain
            )
            if self._on_result:
                try:
                    self._on_result(result, role)
                except Exception:
                    pass  # Never let observability hooks crash governance
            return result
        except GovernanceBlock:
            raise  # Surface to LangChain → chain.invoke() caller
        except Exception as exc:
            logger.warning(
                f"RaiTrustCallbackHandler: evaluation error "
                f"role={role.value} run_id={run_id}: {exc}"
            )
            if self._raise_errors:
                raise
            return None

    # ── LangChain callback hooks ───────────────────────────────────────────────

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Evaluate chat model input as USER_PROMPT at the outer edge."""
        if not self._is_outer(parent_run_id):
            return
        self._evaluate(
            content=self._extract_from_messages(messages),
            role=ContentRole.USER_PROMPT,
            run_id=run_id,
            metadata={"event": "on_chat_model_start"},
        )

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Evaluate raw LLM prompt as USER_PROMPT at the outer edge."""
        if not self._is_outer(parent_run_id):
            return
        content = "\n".join(p for p in prompts if p.strip())
        self._evaluate(
            content=content,
            role=ContentRole.USER_PROMPT,
            run_id=run_id,
            metadata={"event": "on_llm_start"},
        )

    @staticmethod
    def _extract_token_usage(response: Any) -> Optional[Dict[str, Any]]:
        """
        Extract token usage from an LLMResult into the agent_tokens shape
        expected by the Policy Engine audit writer.

        Tries three locations in priority order:
          1. response.llm_output["token_usage"]   — OpenAI / most providers
          2. response.llm_output["usage"]          — Anthropic / Groq via litellm
          3. generation.generation_info            — per-generation fallback

        Returns None when no usage data is found so the metadata key is
        omitted entirely rather than written as zeros.
        """
        llm_out = getattr(response, "llm_output", None) or {}
        usage: Dict[str, Any] = (
            llm_out.get("token_usage")
            or llm_out.get("usage")
            or {}
        )
        # Per-generation fallback (Groq, Together, etc. sometimes put it here)
        if not usage:
            for gen_list in getattr(response, "generations", []):
                for gen in gen_list:
                    gi = getattr(gen, "generation_info", None) or {}
                    if gi.get("usage") or gi.get("token_usage"):
                        usage = gi.get("usage") or gi.get("token_usage") or {}
                        break
                if usage:
                    break

        if not usage:
            return None

        # Normalise to the agent_tokens schema (prompt / completion / total / model)
        prompt     = usage.get("prompt_tokens")     or usage.get("input_tokens")      or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens")     or 0
        total      = usage.get("total_tokens")      or (prompt + completion)
        model_name = llm_out.get("model_name") or llm_out.get("model") or ""

        if total == 0:
            return None

        return {
            "prompt_tokens":     int(prompt),
            "completion_tokens": int(completion),
            "total_tokens":      int(total),
            "model":             model_name,
        }

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Evaluate LLM output as LLM_RESPONSE at the outer edge."""
        if not self._is_outer(parent_run_id):
            return
        meta: Dict[str, Any] = {"event": "on_llm_end"}
        token_usage = self._extract_token_usage(response)
        if token_usage:
            meta["agent_tokens"] = token_usage
        self._evaluate(
            content=self._extract_from_result(response),
            role=ContentRole.LLM_RESPONSE,
            run_id=run_id,
            metadata=meta,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """Pass-through — LLM errors are not governance events."""
        pass

    def on_chain_start(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_chain_end(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_chain_error(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_tool_end(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_tool_error(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_agent_action(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on_agent_finish(self, *args: Any, **kwargs: Any) -> None:
        pass
