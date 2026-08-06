from __future__ import annotations

from core.models.metric_result import MetricResult
from core.store.base import AbstractStore


class PostgresStore(AbstractStore):
    """PostgreSQL backend for production deployments.

    Requires:
        pip install sqlalchemy[asyncio] asyncpg
        DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname in .env
    """

    def init_db(self) -> None:
        raise NotImplementedError("Wire in SQLAlchemy async engine + create_all here.")

    def insert(self, result: MetricResult) -> None:
        raise NotImplementedError

    def latest(self, metric_name: str) -> MetricResult | None:
        raise NotImplementedError

    def latest_all(self) -> list[MetricResult]:
        raise NotImplementedError

    def recent(self, hours: int = 24) -> list[MetricResult]:
        raise NotImplementedError
