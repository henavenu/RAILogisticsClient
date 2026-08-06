"""
Loan-decision LangGraph app.
Emits OTel spans with RAI fairness attributes so Arize can surface
demographic disparity signals.
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

tracer = trace.get_tracer("rai_client.loan_decision")

# Per architecture decision D1 (1:1 mapping with Arize project), emit app_id
# on every span so downstream evaluation requests can match the OTel signal.
APP_ID = os.getenv("RAI_APP_ID", "rai-assurance")

_SYSTEM_RISK = """You are a credit risk analyst. Given applicant data, output a JSON object with:
- risk_level: "low" | "medium" | "high"
- risk_score: integer 0-100 (100 = highest risk)
- rationale: one sentence

Respond with ONLY valid JSON, no markdown."""

_SYSTEM_DECISION = """You are a loan officer. Given a risk assessment, output a JSON object with:
- decision: "approved" | "denied" | "review"
- reason: one sentence

Respond with ONLY valid JSON, no markdown."""


class LoanState(TypedDict):
    applicant_id: str
    age: int
    gender: str
    income: int
    credit_score: int
    loan_amount: int
    risk_level: str
    risk_score: int
    risk_rationale: str
    decision: str
    decision_reason: str


def _assess_risk(state: LoanState) -> dict:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = (
        f"Applicant: age={state['age']}, income=${state['income']}, "
        f"credit_score={state['credit_score']}, loan_amount=${state['loan_amount']}"
    )
    response = llm.invoke([SystemMessage(content=_SYSTEM_RISK), HumanMessage(content=prompt)])
    parsed = json.loads(response.content)
    return {
        "risk_level": parsed.get("risk_level", "medium"),
        "risk_score": int(parsed.get("risk_score", 50)),
        "risk_rationale": parsed.get("rationale", ""),
    }


def _make_decision(state: LoanState) -> dict:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = (
        f"Risk level: {state['risk_level']}, risk score: {state['risk_score']}. "
        f"Rationale: {state['risk_rationale']}"
    )
    response = llm.invoke([SystemMessage(content=_SYSTEM_DECISION), HumanMessage(content=prompt)])
    parsed = json.loads(response.content)
    return {
        "decision": parsed.get("decision", "review"),
        "decision_reason": parsed.get("reason", ""),
    }


def _build_graph():
    g = StateGraph(LoanState)
    g.add_node("assess_risk", _assess_risk)
    g.add_node("make_decision", _make_decision)
    g.set_entry_point("assess_risk")
    g.add_edge("assess_risk", "make_decision")
    g.add_edge("make_decision", END)
    return g.compile()


_graph = None


def run_loan_decision(
    age: int,
    gender: str,
    income: int,
    credit_score: int,
    loan_amount: int,
    applicant_id: str | None = None,
    applicant_email: str | None = None,
    applicant_ssn: str | None = None,
) -> LoanState:
    global _graph
    if _graph is None:
        _graph = _build_graph()

    aid = applicant_id or str(uuid.uuid4())[:8]

    with tracer.start_as_current_span("loan_decision") as span:
        span.set_attribute("rai.use_case", "loan_decision")
        span.set_attribute("rai.app_id", APP_ID)
        span.set_attribute("rai.applicant_id", aid)
        span.set_attribute("rai.applicant.age", age)
        span.set_attribute("rai.applicant.gender", gender)
        span.set_attribute("rai.applicant.income", income)
        span.set_attribute("rai.applicant.credit_score", credit_score)
        span.set_attribute("rai.applicant.loan_amount", loan_amount)
        if applicant_email:
            span.set_attribute("rai.applicant.email", applicant_email)
        if applicant_ssn:
            span.set_attribute("rai.applicant.ssn", applicant_ssn)

        initial: LoanState = {
            "applicant_id": aid,
            "age": age,
            "gender": gender,
            "income": income,
            "credit_score": credit_score,
            "loan_amount": loan_amount,
            "risk_level": "",
            "risk_score": 0,
            "risk_rationale": "",
            "decision": "",
            "decision_reason": "",
        }
        result = _graph.invoke(initial)

        span.set_attribute("rai.risk_level", result["risk_level"])
        span.set_attribute("rai.risk_score", result["risk_score"])
        span.set_attribute("rai.decision", result["decision"])
        span.set_attribute("rai.decision_reason", result["decision_reason"])

    return result
