from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.models.metric_result import MetricResult
from core.store.base import AbstractStore

_DB_PATH = Path("data/metrics.db")


class SqliteStore(AbstractStore):

    def _conn(self) -> sqlite3.Connection:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(_DB_PATH, check_same_thread=False)

    def init_db(self) -> None:
        with self._conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS metric_results (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name   TEXT    NOT NULL,
                    value         REAL    NOT NULL,
                    unit          TEXT,
                    status        TEXT,
                    threshold     REAL,
                    project_name  TEXT,
                    span_count    INTEGER,
                    breakdown     TEXT,
                    timestamp     TEXT    NOT NULL
                )
            """)

    def insert(self, result: MetricResult) -> None:
        with self._conn() as con:
            con.execute(
                """INSERT INTO metric_results
                   (metric_name, value, unit, status, threshold,
                    project_name, span_count, breakdown, timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    result.metric_name, result.value, result.unit,
                    result.status, result.threshold, result.project_name,
                    result.span_count, json.dumps(result.breakdown),
                    result.timestamp.isoformat(),
                ),
            )

    def latest(self, metric_name: str) -> MetricResult | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM metric_results WHERE metric_name=? ORDER BY timestamp DESC LIMIT 1",
                (metric_name,),
            ).fetchone()
        return self._to_metric(row) if row else None

    def latest_all(self) -> list[MetricResult]:
        with self._conn() as con:
            rows = con.execute("""
                SELECT * FROM metric_results
                WHERE id IN (
                    SELECT MAX(id) FROM metric_results GROUP BY metric_name
                )
                ORDER BY metric_name
            """).fetchall()
        return [self._to_metric(r) for r in rows]

    def recent(self, hours: int = 24) -> list[MetricResult]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM metric_results WHERE timestamp >= ? ORDER BY timestamp DESC",
                (cutoff,),
            ).fetchall()
        return [self._to_metric(r) for r in rows]

    def _to_metric(self, row: tuple) -> MetricResult:
        _, metric_name, value, unit, status, threshold, project_name, span_count, breakdown, ts = row
        return MetricResult(
            metric_name=metric_name,
            value=value,
            unit=unit or "",
            status=status or "ok",
            threshold=threshold,
            project_name=project_name or "",
            span_count=span_count or 0,
            breakdown=json.loads(breakdown or "{}"),
            timestamp=datetime.fromisoformat(ts),
        )
