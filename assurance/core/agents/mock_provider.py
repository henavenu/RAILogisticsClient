from __future__ import annotations

from core.agents.base import BiasDetectionProvider, BiasGroupMetric, BiasReport


class MockBiasProvider(BiasDetectionProvider):
    """Offline provider for tests. Returns a fixed BiasReport without external calls."""

    name = "mock"

    def is_available(self) -> bool:
        return True

    def detect(
        self,
        records: list[dict],
        outcome_col: str = "decision",
        favorable_outcome: str = "Approved",
    ) -> BiasReport:
        metric = BiasGroupMetric(
            protected_attribute="gender",
            privileged_group="Male",
            unprivileged_group="Female",
            privileged_rate=0.74,
            unprivileged_rate=0.54,
            metric_name="DIR",
            metric_value=0.73,
            threshold=0.80,
            passed=False,
            interpretation="DIR=0.73 below 4/5 threshold (0.80) — disparate impact.",
            severity="HIGH",
        )
        return BiasReport(
            provider=self.name,
            overall_passed=False,
            risk_level="HIGH",
            metrics=[metric],
            violations=["GENDER (Female vs Male): DIR=0.730 — disparate impact."],
            recommendations=["Audit gender features — remove proxies (e.g., title, first name)."],
            records_analyzed=len(records),
            elapsed_ms=0.1,
        )
