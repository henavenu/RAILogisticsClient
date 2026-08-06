"""
MyBank — LangGraph state definition.
"""
from __future__ import annotations

from typing import Annotated, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class BankingState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    customer_id: str
    session_id: str
    intent: str          # "account" | "transfer" | "profile" | "card_search" | "unknown"
    transfer_type: str   # "domestic" | "international" | "" (set by transfer_router)
    card_search_cache: Optional[dict]  # (legacy, card-search flow removed) retained
                                       # so any residual references resolve harmlessly
    # ── Payment-council poll loop (Stage 3: real coordinator + reviewer nodes) ──
    # The coordinator dispatches the 4 reviewer nodes one at a time, for
    # _COUNCIL_POLL_ROUNDS rounds, via Command(goto). These carry the loop cursor.
    council_round: int                 # current polling round (1-based)
    council_idx: int                   # index of the next reviewer to poll this round
