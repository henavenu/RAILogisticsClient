from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class BiasGroupMetric(BaseModel):
    protected_attribute: str
    privileged_group: str
    unprivileged_group: str
    privileged_rate: float = 0.0
    unprivileged_rate: float = 0.0
    metric_name: str
    metric_value: float
    threshold: float = 0.0
    passed: bool
    interpretation: str = ""
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"


class BiasReport(BaseModel):
    provider: str
    overall_passed: bool
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    metrics: list[BiasGroupMetric]
    violations: list[str] = []
    recommendations: list[str] = []
    records_analyzed: int = 0
    elapsed_ms: float | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BiasDetectionProvider(ABC):
    """SPI for pluggable bias detection backends.

    Implementations may call out to a remote service,
    run a local statistical computation, or invoke an LLM-as-judge.
    """

    name: str = "BaseBiasDetectionProvider"

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend is reachable and configured."""

    @abstractmethod
    def detect(
        self,
        records: list[dict],
        outcome_col: str = "decision",
        favorable_outcome: str = "Approved",
    ) -> BiasReport:
        """Analyze a list of decision records and return bias findings."""
