"""
Customer-support LangGraph app.
Emits OTel spans with RAI safety attributes (toxicity flag, sentiment, intent)
so Arize can surface content-safety signals.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from opentelemetry import trace

tracer = trace.get_tracer("rai_client.support_chat")

# Per architecture decision D1 (1:1 mapping with Arize project), emit app_id
# on every span so downstream evaluation requests can match the OTel signal.
APP_ID = os.getenv("RAI_APP_ID", "rai-assurance")

_SYSTEM_CLASSIFY = """You are a content safety and intent classifier. Given a customer message, output a JSON object with:
- intent: "billing" | "technical" | "complaint" | "general" | "escalation"
- sentiment: "positive" | "neutral" | "negative"
- toxicity_flagged: true | false
- safety_category: "safe" | "offensive" | "pii" | "threat"

Respond with ONLY valid JSON, no markdown."""

_SYSTEM_RESPOND = """You are a helpful customer support agent for a financial services company.
Respond professionally, empathetically, and concisely.
Keep the response under 3 sentences."""


class SupportState(TypedDict):
    session_id: str
    user_message: str
    intent: str
    sentiment: str
    toxicity_flagged: bool
    safety_category: str
    response: str


def _classify_intent(state: SupportState) -> dict:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    response = llm.invoke([
        SystemMessage(content=_SYSTEM_CLASSIFY),
        HumanMessage(content=state["user_message"]),
    ])
    parsed = json.loads(response.content)
    return {
        "intent": parsed.get("intent", "general"),
        "sentiment": parsed.get("sentiment", "neutral"),
        "toxicity_flagged": bool(parsed.get("toxicity_flagged", False)),
        "safety_category": parsed.get("safety_category", "safe"),
    }


def _generate_response(state: SupportState) -> dict:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)
    context = (
        f"[Intent: {state['intent']}, Sentiment: {state['sentiment']}]\n"
        f"Customer: {state['user_message']}"
    )
    response = llm.invoke([
        SystemMessage(content=_SYSTEM_RESPOND),
        HumanMessage(content=context),
    ])
    return {"response": response.content}


def _build_graph():
    g = StateGraph(SupportState)
    g.add_node("classify_intent", _classify_intent)
    g.add_node("generate_response", _generate_response)
    g.set_entry_point("classify_intent")
    g.add_edge("classify_intent", "generate_response")
    g.add_edge("generate_response", END)
    return g.compile()


_graph = None


def run_support_chat(
    user_message: str,
    session_id: str | None = None,
) -> SupportState:
    global _graph
    if _graph is None:
        _graph = _build_graph()

    sid = session_id or str(uuid.uuid4())[:8]

    with tracer.start_as_current_span("support_chat") as span:
        span.set_attribute("rai.use_case", "support_chat")
        span.set_attribute("rai.app_id", APP_ID)
        span.set_attribute("rai.session_id", sid)
        span.set_attribute("rai.user_message", user_message)
        span.set_attribute("rai.user_message_len", len(user_message))

        initial: SupportState = {
            "session_id": sid,
            "user_message": user_message,
            "intent": "",
            "sentiment": "",
            "toxicity_flagged": False,
            "safety_category": "safe",
            "response": "",
        }
        result = _graph.invoke(initial)

        span.set_attribute("rai.intent", result["intent"])
        span.set_attribute("rai.sentiment", result["sentiment"])
        span.set_attribute("rai.toxicity_flagged", result["toxicity_flagged"])
        span.set_attribute("rai.safety_category", result["safety_category"])

    return result
