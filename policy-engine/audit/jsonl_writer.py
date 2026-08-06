"""
JsonlAuditWriter — appends one JSON line per evaluation event.

Format: each line is a complete, independently-parseable JSON object
conforming to the A1UC-1 audit record schema.

Thread-safety: writes are serialised through a threading.Lock so
concurrent evaluate() calls never interleave partial records.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

from audit.writer import AuditWriter, AuditRecord


class JsonlAuditWriter(AuditWriter):
    """Thread-safe append-only JSONL writer."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
