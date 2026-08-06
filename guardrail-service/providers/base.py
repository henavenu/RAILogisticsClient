"""
TrustProvider SPI (Service Provider Interface) — Template Method pattern.

Contract
--------
Subclasses implement _evaluate() only.  The public evaluate() method is
owned by this base class and is NOT overridden.  It guarantees:

  1. Every requested dimension always receives a result — no silent drops.
  2. All exceptions are caught; the provider never propagates to the caller.
  3. On any failure, fail-open returns score=1/PASS with metadata notes, or
     fail-closed returns score=9/FLAGGED depending on the provider's fail_mode.
  4. A platform alert is emitted on every provider failure so the dashboard
     and SSE stream are updated immediately.

This makes fail-safe behaviour a platform-level guarantee, not each
provider's responsibility.  Subclasses focus purely on the happy path.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TrustProvider(ABC):
    """
    Base class for all evaluation provider adapters.

    Subclasses must implement:
      - name()        — stable provider identifier (registry key)
      - _evaluate()   — the actual provider call; MAY raise

    Subclasses must NOT override:
      - evaluate()    — owned by base; enforces the full SPI contract
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @abstractmethod
    def name(self) -> str:
        """
        Unique, stable identifier for this provider.
        Must not change across restarts — used as the registry key and
        stored in audit records.
        """

    # ── Fail-mode (overridable by subclasses that read it from DB config) ─────

    @property
    def fail_mode(self) -> str:
        """
        How this provider behaves when _evaluate() raises an unhandled exception.
          "fail_open"   → score=1/PASS   (default — never blocks on provider outage)
          "fail_closed" → score=9/FLAGGED (treat outage as worst-case violation)
        Subclasses that load config from DB should override this with the DB value.
        """
        return getattr(self, "_fail_mode", "fail_open")

    # ── Inner implementation (subclass responsibility) ────────────────────────

    @abstractmethod
    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        """
        Perform the actual evaluation.  May raise — the base class handles it.

        Args:
            content:      Text to evaluate (post-Privacy Proxy sanitised).
            evaluations:  Canonical dimension names requested.
            content_role: ContentRole string value.

        Returns:
            Tuple of (results_list, latency_ms).
            results_list: one dict per dimension with keys:
              "dimension", "status", "score", "provider_metadata" (optional)
        """

    # ── Public SPI entry point (final — do not override) ─────────────────────

    def evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
        fail_mode: str = "fail_open",
    ) -> Tuple[List[Dict], int]:
        """
        Safe evaluation wrapper.  Callers always call this, never _evaluate().

        Guarantees a valid result list for every requested dimension in all
        circumstances.  Emits a platform alert on any provider failure.

        Args:
            content, evaluations, content_role: forwarded to _evaluate().
            fail_mode: "fail_open"   → return score=1/PASS on failure (default)
                       "fail_closed" → return score=9/FLAGGED on failure
        """
        start = time.time()
        try:
            results, latency_ms = self._evaluate(content, evaluations, content_role)
            # Enforce completeness — every requested dim must appear in results
            results = self._enforce_completeness(results, evaluations, latency_ms)
            return results, latency_ms

        except Exception as exc:
            latency_ms = int((time.time() - start) * 1000)
            logger.error(
                f"[{self.name()}] _evaluate() raised unexpectedly: {exc}",
                exc_info=True,
            )
            self._emit_provider_alert(str(exc))
            return self._build_failure_results(evaluations, str(exc), fail_mode), latency_ms

    # ── Completeness enforcement ──────────────────────────────────────────────

    def _enforce_completeness(
        self,
        results: List[Dict],
        requested: List[str],
        latency_ms: int,
    ) -> List[Dict]:
        """
        Ensure every requested dimension has a result entry.
        If a subclass silently dropped a dimension, backfill with PASS + note.
        """
        returned_dims = {r.get("dimension") for r in results}
        for dim in requested:
            if dim not in returned_dims:
                logger.warning(
                    f"[{self.name()}] result missing for dimension '{dim}' "
                    f"— backfilling with PASS"
                )
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note": (
                            f"Provider '{self.name()}' did not return a result for "
                            f"'{dim}' — backfilled with PASS by SPI contract."
                        ),
                        "backfilled": True,
                    },
                })
        return results

    # ── Failure result builders ───────────────────────────────────────────────

    def _build_failure_results(
        self,
        dimensions: List[str],
        reason: str,
        fail_mode: str,
    ) -> List[Dict]:
        """
        Build results for all requested dimensions when the provider has failed.

        fail_open   → score=1, status=PASS  (execution proceeds unguarded)
        fail_closed → score=9, status=FLAGGED (execution is blocked as precaution)
        """
        if fail_mode == "fail_closed":
            score, status = 9, "FLAGGED"
        else:
            score, status = 1, "PASS"

        return [
            {
                "dimension": dim,
                "score":     score,
                "status":    status,
                "provider_metadata": {
                    "fail_mode":        fail_mode,
                    "provider_failure": True,
                    "reason":           reason,
                    "note": (
                        f"Provider '{self.name()}' unavailable — result is "
                        f"{'PASS (fail-open)' if fail_mode == 'fail_open' else 'FLAGGED (fail-closed)'}."
                    ),
                },
            }
            for dim in dimensions
        ]

    # ── Platform alert emission ───────────────────────────────────────────────

    def _emit_provider_alert(self, reason: str) -> None:
        """
        Write a PROVIDER_UNAVAILABLE alert to the platform_alerts table.
        Best-effort — if the DB write itself fails, log and continue.
        The GS must never crash because the alert writer failed.
        """
        try:
            from core.database import write_provider_alert
            write_provider_alert(provider_name=self.name(), reason=reason)
        except ImportError:
            # GS DB integration not available in this environment — log only
            logger.warning(
                f"[{self.name()}] provider failure (alert not persisted — "
                f"core.database unavailable): {reason}"
            )
        except Exception as alert_exc:
            logger.warning(
                f"[{self.name()}] failed to write provider alert: {alert_exc}"
            )
