from __future__ import annotations

import json
import time
from pathlib import Path

from core.providers.base import AssuranceProvider

_JSONL_PATH = Path("data/spans.jsonl")


def write_span_record(name: str, attributes: dict, start_ns: int, end_ns: int) -> None:
    _JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "name": name,
        "start_time": start_ns,
        "end_time": end_ns,
        "attributes": attributes,
        "status": "OK",
    }
    with _JSONL_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


class LocalProvider(AssuranceProvider):

    def __init__(self, spans_path: str = "data/spans.jsonl") -> None:
        self._path = Path(spans_path)

    def is_available(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def fetch_spans(
        self,
        project_name: str,
        use_case: str,
        since_hours: int = 1,
    ) -> list[dict]:
        if not self.is_available():
            return []

        cutoff_ns = (time.time() - since_hours * 3600) * 1_000_000_000
        spans = []
        with self._path.open() as f:
            for line in f:
                span = json.loads(line)
                if span["name"] == use_case and span.get("start_time", 0) >= cutoff_ns:
                    spans.append(span)
        return spans
