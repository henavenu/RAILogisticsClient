"""Pydantic response schemas for the Trust Agents API."""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel


class DriftSignal(BaseModel):
    signal_id:        str
    app_id:           str
    metric_path:      str
    signal_type:      str
    direction:        str
    first_seen_at:    str
    last_seen_at:     str
    resolved_at:      Optional[str]
    threshold_green:  Optional[float]
    threshold_amber:  Optional[float]
    current_value:    Optional[float]
    consecutive_runs: int


class Finding(BaseModel):
    finding_id:      str
    run_id:          str
    agent_type:      str
    app_id:          str
    generated_at:    str
    overall_status:  str
    severity:        str
    summary_text:    Optional[str]
    reasoning_trace: Optional[str]
    findings_json:   Optional[Any]
    context_runs:    int


class FindingsResponse(BaseModel):
    compliance: Optional[Finding]
    tracker:    Optional[Finding]
    drift:      list[DriftSignal]


class RunResponse(BaseModel):
    finding_id:     str
    run_id:         str
    agent_type:     str
    overall_status: str
    severity:       str
    summary:        Optional[str]
