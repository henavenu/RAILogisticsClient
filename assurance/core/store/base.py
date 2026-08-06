from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.metric_result import MetricResult


class AbstractStore(ABC):

    @abstractmethod
    def init_db(self) -> None: ...

    @abstractmethod
    def insert(self, result: MetricResult) -> None: ...

    @abstractmethod
    def latest(self, metric_name: str) -> MetricResult | None: ...

    @abstractmethod
    def latest_all(self) -> list[MetricResult]: ...

    @abstractmethod
    def recent(self, hours: int = 24) -> list[MetricResult]: ...
