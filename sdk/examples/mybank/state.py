"""
MyBank — LangGraph state definition.
"""
from __future__ import annotations

from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class BankingState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    customer_id: str
    session_id: str
    intent: str       # "account" | "transfer" | "profile" | "unknown"
    eval_text: str    # Privacy-proxy-sanitized version of last user message.
                      # Set by router_node once; used by specialist nodes for
                      # A2A evaluations so PII is never sent to the PE twice.
