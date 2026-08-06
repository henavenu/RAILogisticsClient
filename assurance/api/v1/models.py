from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


DataStatus = Literal["ok", "no_data", "provider_unavailable", "unsupported", "experimental"]
Window = Literal["1h", "6h", "24h", "7d"]
Unit = Literal["%", "score", "count", "ms", ""]


class MetricItem(BaseModel):
    metric_id: str
    value: float | None
    unit: Unit = ""
    data_status: DataStatus
    error: str | None = None


class MetricsResponse(BaseModel):
    app_id: str
    window: str   # enum preset (e.g. "24h") or ISO range "<since>__<until>"
    data_status: DataStatus
    metric_count: int
    metrics: list[MetricItem]


class CatalogItem(BaseModel):
    metric_id: str
    unit: Unit


class CatalogResponse(BaseModel):
    metric_count: int
    metrics: list[CatalogItem]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    provider_status: Literal["reachable", "unreachable", "unknown"]
