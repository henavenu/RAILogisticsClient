"""
Guardrail Service inbound request schema.
Matches SRS §4.6 TrustEvaluateRequest.
"""
from typing import List, Optional
from pydantic import BaseModel, Field
from core.enums import ContentRole


class TrustEvaluateRequest(BaseModel):
    request_id:   str         = Field(..., description="OTel Trace ID from the calling SDK")
    content:      str         = Field(..., description="Post-Privacy Proxy sanitised content")
    content_role: ContentRole = Field(..., description="user_prompt | llm_response | agent_to_agent")
    evaluations:  Optional[List[str]] = Field(
        None,
        description="Explicit list of dimension names to evaluate. "
                    "If null, all active dimensions applicable to the role are evaluated.",
    )
