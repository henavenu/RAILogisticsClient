from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class MetricResult(BaseModel):
    metric_id: str
    value: float
    unit: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    project_name: str
    span_count: int
    breakdown: dict[str, float] = {}

    def summary(self) -> str:
        parts = [f"{self.metric_id}={self.value:.3f}"]
        if self.breakdown:
            parts.append("  breakdown: " + ", ".join(f"{k}={v:.2f}" for k, v in self.breakdown.items()))
        parts.append(f"  spans={self.span_count}  project={self.project_name}")
        return "\n".join(parts)
