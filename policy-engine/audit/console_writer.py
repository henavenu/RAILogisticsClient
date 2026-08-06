"""
ConsoleAuditWriter — Rich-formatted, colour-coded evaluation output.

Verdict colours:
  PERMISSIVE  bold green
  NORMATIVE   bold yellow
  COERCIVE    bold red

Each record is rendered as a single line with optional violation detail
on a second indented line so logs stay readable under high volume.
"""
from __future__ import annotations

import threading

from rich.console import Console
from rich.text import Text

from audit.writer import AuditWriter, AuditRecord

_VERDICT_STYLE: dict[str, str] = {
    "PERMISSIVE": "bold green",
    "NORMATIVE":  "bold yellow",
    "COERCIVE":   "bold red",
}


class ConsoleAuditWriter(AuditWriter):
    """Thread-safe Rich console writer."""

    def __init__(self) -> None:
        self._console = Console(highlight=False, markup=False)
        self._lock    = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        style = _VERDICT_STYLE.get(record.verdict, "white")
        ts    = record.timestamp[:19].replace("T", " ")

        session_tag = ""
        if record.session_id:
            turn = f"  turn={record.turn_number}" if record.turn_number else ""
            escalated = "  [SESSION-ESCALATED]" if record.session_escalated else ""
            session_tag = f"  sess={record.session_id[:12]}{turn}{escalated}"

        line = Text()
        line.append("[AUDIT] ", style="dim")
        line.append(f"{ts}  ", style="dim")
        line.append(f"{record.verdict:<12}", style=style)
        line.append(f"  {record.latency_ms:6.0f}ms  ", style="cyan")
        line.append(f"app={record.app_id}  role={record.role}  ", style="dim")
        line.append(f"req={record.request_id[:16]}", style="dim")
        if session_tag:
            line.append(session_tag, style="dim magenta")

        if record.fail_reason:
            line.append(
                f"\n         [fail_reason={record.fail_reason}]",
                style="bold yellow",
            )
        elif record.violations:
            snippet = "; ".join(v[:80] for v in record.violations[:2])
            line.append(f"\n         violations: {snippet}", style="italic dim red")

        with self._lock:
            self._console.print(line)
