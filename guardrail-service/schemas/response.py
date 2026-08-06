"""
Guardrail Service response schemas.
Matches SRS §4.6 TrustEvaluateResponse, ProviderResult, EvaluationResult.
"""
from typing import List, Optional
from pydantic import BaseModel


class EvaluationResult(BaseModel):
    dimension:         str
    status:            str            # PASS | WARNING | FLAGGED | UNKNOWN
    score:             Optional[int]  # 1-9 canonical integer scale; None if UNKNOWN
    provider_metadata: Optional[dict] = None


class ProviderResult(BaseModel):
    provider:    str
    latency_ms:  int
    evaluations: List[EvaluationResult]


class TrustEvaluateResponse(BaseModel):
    request_id:       str
    provider_results: List[ProviderResult]
    errors:           Optional[List[dict]] = None
