from __future__ import annotations

import time

from core.providers.base import AssuranceProvider

_LOAN_SPANS = [
    {"name": "loan_decision", "attributes": {"rai.applicant.gender": "female",     "rai.decision": "approved", "rai.risk_level": "low",    "rai.risk_score": 25, "rai.applicant.age": 32}, "start_time": 0, "end_time": 2_000_000_000},
    {"name": "loan_decision", "attributes": {"rai.applicant.gender": "male",       "rai.decision": "approved", "rai.risk_level": "low",    "rai.risk_score": 20, "rai.applicant.age": 45}, "start_time": 0, "end_time": 2_000_000_000},
    {"name": "loan_decision", "attributes": {"rai.applicant.gender": "female",     "rai.decision": "approved", "rai.risk_level": "low",    "rai.risk_score": 30, "rai.applicant.age": 28}, "start_time": 0, "end_time": 2_000_000_000},
    {"name": "loan_decision", "attributes": {"rai.applicant.gender": "male",       "rai.decision": "approved", "rai.risk_level": "low",    "rai.risk_score": 22, "rai.applicant.age": 55}, "start_time": 0, "end_time": 2_000_000_000},
    {"name": "loan_decision", "attributes": {"rai.applicant.gender": "non-binary", "rai.decision": "denied",   "rai.risk_level": "high",   "rai.risk_score": 80, "rai.applicant.age": 23}, "start_time": 0, "end_time": 2_000_000_000},
    {"name": "loan_decision", "attributes": {"rai.applicant.gender": "female",     "rai.decision": "approved", "rai.risk_level": "low",    "rai.risk_score": 28, "rai.applicant.age": 38}, "start_time": 0, "end_time": 2_000_000_000},
]

_SUPPORT_SPANS = [
    {"name": "support_chat", "attributes": {"rai.intent": "billing",    "rai.sentiment": "negative", "rai.toxicity_flagged": False, "rai.safety_category": "safe"},      "start_time": 0, "end_time": 1_000_000_000},
    {"name": "support_chat", "attributes": {"rai.intent": "technical",  "rai.sentiment": "negative", "rai.toxicity_flagged": False, "rai.safety_category": "safe"},      "start_time": 0, "end_time": 1_000_000_000},
    {"name": "support_chat", "attributes": {"rai.intent": "complaint",  "rai.sentiment": "negative", "rai.toxicity_flagged": True,  "rai.safety_category": "offensive"}, "start_time": 0, "end_time": 1_000_000_000},
    {"name": "support_chat", "attributes": {"rai.intent": "general",    "rai.sentiment": "neutral",  "rai.toxicity_flagged": False, "rai.safety_category": "safe"},      "start_time": 0, "end_time": 1_000_000_000},
    {"name": "support_chat", "attributes": {"rai.intent": "escalation", "rai.sentiment": "negative", "rai.toxicity_flagged": False, "rai.safety_category": "safe"},      "start_time": 0, "end_time": 1_000_000_000},
    {"name": "support_chat", "attributes": {"rai.intent": "general",    "rai.sentiment": "neutral",  "rai.toxicity_flagged": False, "rai.safety_category": "safe"},      "start_time": 0, "end_time": 1_000_000_000},
]


class MockProvider(AssuranceProvider):

    def is_available(self) -> bool:
        return True

    def fetch_spans(
        self,
        project_name: str,
        use_case: str,
        since_hours: int = 1,
    ) -> list[dict]:
        now_ns = int(time.time() * 1_000_000_000)
        if use_case == "loan_decision":
            spans = _LOAN_SPANS
        elif use_case == "support_chat":
            spans = _SUPPORT_SPANS
        else:
            return []
        return [{**s, "start_time": now_ns - 1_000_000_000, "end_time": now_ns} for s in spans]
