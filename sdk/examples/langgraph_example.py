"""
Example: RAI Trust SDK with LangGraph

Shows explicit node-level governance via LangGraphGuard.wrap().

The guard decorator:
  • Evaluates the state's "messages" field as USER_PROMPT before the node runs
  • Evaluates the output messages as LLM_RESPONSE after the node runs
  • Raises GovernanceBlock (SYNC mode) if the PE returns COERCIVE

This is the preferred pattern for LangGraph because:
  1. Governance intent is visible in the graph definition
  2. Enforcement fires before OR after the node (not after-the-fact like callbacks)
  3. Works naturally with LangGraph checkpointing

Install:
    pip install langgraph langchain-openai
    pip install 'rai-trust-sdk[langgraph]'
"""
import os
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from rai_trust_sdk import GovernanceBlock, TrustClient
from rai_trust_sdk.langgraph import LangGraphGuard
from rai_trust_sdk.types import ContentRole, EvalMode


# ── 1. State schema ───────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ── 2. Trust infrastructure ────────────────────────────────────────────────────

client = TrustClient(
    base_url=os.environ.get("TRUST_PE_URL", "http://localhost:3000"),
    app_id="finapp",
)
guard = LangGraphGuard(
    client=client,
    eval_mode=EvalMode.SYNC,
    on_result=lambda r, role: print(
        f"[Trust] role={role.value} verdict={r.verdict.value} "
        f"violations={r.violations}"
    ),
)

# ── 3. Nodes ───────────────────────────────────────────────────────────────────

llm = ChatOpenAI(model="gpt-4o-mini")


@guard.wrap(
    input_role=ContentRole.USER_PROMPT,    # evaluate incoming messages
    output_role=ContentRole.LLM_RESPONSE,  # evaluate outgoing messages
    content_key="messages",
)
def llm_node(state: AgentState) -> AgentState:
    """Standard LLM inference node, wrapped with governance."""
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


# ── 4. Graph ───────────────────────────────────────────────────────────────────

workflow = StateGraph(AgentState)
workflow.add_node("llm", llm_node)
workflow.set_entry_point("llm")
workflow.add_edge("llm", END)

app = workflow.compile()

# ── 5. Run ─────────────────────────────────────────────────────────────────────

try:
    result = app.invoke({
        "messages": [HumanMessage(content="Summarise recent AML regulations.")],
    })
    print("Final message:", result["messages"][-1].content)

except GovernanceBlock as block:
    print(f"\nGovernance block: {block}")
    print(f"Violations:    {block.result.violations}")
    print(f"Remediations:  {block.result.remediations}")
    print(f"Pack trace:    {block.result.pack_trace}")


# ── 6. Multi-turn session example ─────────────────────────────────────────────

import uuid

SESSION_ID = str(uuid.uuid4())

# Create a session-aware guard — PE accumulates scores across turns
session_guard = LangGraphGuard(
    client=client,
    session_id=SESSION_ID,
    eval_mode=EvalMode.SYNC,
)


@session_guard.wrap(content_key="messages")
def session_llm_node(state: AgentState) -> AgentState:
    response = llm.invoke(state["messages"])
    return {"messages": [response]}
