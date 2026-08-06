"""
App Trust Tracker — per-application rolling trust score.

Two implementations are provided:

  DbAppTrustTracker (preferred)
    Reads/writes pe_app_trust_scores in the shared PAP SQLite database.
    Survives PE restarts.  PAP dashboard can query scores without an HTTP call.

  AppTrustTracker (legacy)
    Reads/writes data/app_trust_scores.json on disk.
    Used only as a fallback when the shared PAP database is unavailable.

Score mechanics (same in both implementations)
────────────────────────────────────────────────
  Initialises at 1.0 on first seen app.
  Deltas:  COERCIVE  -0.20  |  NORMATIVE  -0.05  |  PERMISSIVE  +0.02
  Bounded [0.20, 1.0].  Last 50 events retained for dashboard sparkline.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DELTA = {"COERCIVE": -0.20, "NORMATIVE": -0.05, "PERMISSIVE": +0.02}
_FLOOR   = 0.20
_CEILING = 1.0
_MAX_EVENTS = 50


def _score_status(score: float) -> str:
    if score >= 0.80:
        return "maintaining"
    if score >= 0.60:
        return "step_up"
    if score >= 0.40:
        return "step_up_plus"
    return "critical"


# ══════════════════════════════════════════════════════════════════════════════
#  DB-backed implementation (preferred)
# ══════════════════════════════════════════════════════════════════════════════

class DbAppTrustTracker:
    """
    Per-app trust tracker backed by the shared PAP SQLite database.

    Writes to pe_app_trust_scores.  PAP dashboard can query scores directly
    without any HTTP call to the PE.
    """

    def __init__(self):
        from core.database import engine
        self._engine = engine
        self._lock   = threading.RLock()
        logger.info("[DbAppTrustTracker] Initialised — persisting to PAP DB pe_app_trust_scores")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_row(self, app_id: str) -> Optional[dict]:
        from sqlalchemy import text
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT score, total_evaluations, coercive_count, normative_count, "
                    "       permissive_count, recent_events_json "
                    "FROM pe_app_trust_scores WHERE app_id = :app_id"
                ),
                {"app_id": app_id},
            ).fetchone()
        if row is None:
            return None
        events_raw = row[5]
        if isinstance(events_raw, str):
            try:
                events_raw = json.loads(events_raw)
            except Exception:
                events_raw = []
        return {
            "score":             row[0],
            "total_evaluations": row[1],
            "coercive_count":    row[2],
            "normative_count":   row[3],
            "permissive_count":  row[4],
            "events":            events_raw or [],
        }

    def _upsert(self, app_id: str, data: dict) -> None:
        from sqlalchemy import text
        events_json = json.dumps(data["events"])
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pe_app_trust_scores "
                    "  (app_id, score, total_evaluations, coercive_count, normative_count, "
                    "   permissive_count, recent_events_json, first_seen_at, last_updated_at) "
                    "VALUES (:app_id, :score, :total, :coercive, :normative, :permissive, "
                    "        :events, datetime('now'), datetime('now')) "
                    "ON CONFLICT(app_id) DO UPDATE SET "
                    "  score = :score, total_evaluations = :total, "
                    "  coercive_count = :coercive, normative_count = :normative, "
                    "  permissive_count = :permissive, recent_events_json = :events, "
                    "  last_updated_at = datetime('now')"
                ),
                {
                    "app_id":    app_id,
                    "score":     data["score"],
                    "total":     data["total_evaluations"],
                    "coercive":  data["coercive_count"],
                    "normative": data["normative_count"],
                    "permissive": data["permissive_count"],
                    "events":    events_json,
                },
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def record(self, app_id: str, verdict: str) -> float:
        verdict = verdict.upper()
        delta   = _DELTA.get(verdict, 0.0)

        with self._lock:
            data = self._get_row(app_id)
            if data is None:
                data = {
                    "score": _CEILING, "total_evaluations": 0,
                    "coercive_count": 0, "normative_count": 0, "permissive_count": 0,
                    "events": [],
                }

            old_score = data["score"]
            new_score = round(max(_FLOOR, min(_CEILING, old_score + delta)), 4)
            data["score"] = new_score
            data["total_evaluations"] += 1

            vkey = {"COERCIVE": "coercive_count", "NORMATIVE": "normative_count",
                    "PERMISSIVE": "permissive_count"}.get(verdict)
            if vkey:
                data[vkey] += 1

            event = {
                "ts":      datetime.now(tz=timezone.utc).isoformat(),
                "verdict": verdict,
                "delta":   delta,
                "score":   new_score,
            }
            data["events"].append(event)
            if len(data["events"]) > _MAX_EVENTS:
                data["events"] = data["events"][-_MAX_EVENTS:]

            self._upsert(app_id, data)

        if delta != 0:
            logger.debug(
                f"[DbAppTrustTracker] app={app_id} verdict={verdict} "
                f"score {old_score:.3f} → {new_score:.3f} (delta={delta:+.2f})"
            )
        return new_score

    def get_score(self, app_id: str) -> Optional[float]:
        with self._lock:
            data = self._get_row(app_id)
            return data["score"] if data else None

    def get_all_stats(self) -> Dict[str, Any]:
        from sqlalchemy import text
        with self._lock:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT app_id, score, total_evaluations, "
                        "       coercive_count, normative_count, permissive_count, "
                        "       recent_events_json "
                        "FROM pe_app_trust_scores"
                    )
                ).fetchall()

        result = {}
        for row in rows:
            app_id   = row[0]
            score    = row[1]
            events_raw = row[6]
            if isinstance(events_raw, str):
                try:
                    events_raw = json.loads(events_raw)
                except Exception:
                    events_raw = []
            result[app_id] = {
                "app_id":            app_id,
                "score":             score,
                "status":            _score_status(score),
                "total_evaluations": row[2],
                "verdict_counts": {
                    "COERCIVE":   row[3],
                    "NORMATIVE":  row[4],
                    "PERMISSIVE": row[5],
                },
                "recent_events": (events_raw or [])[-10:],
            }
        return result

    # ── Admin API (used by reset endpoint in main.py) ─────────────────────────

    def reset(self, app_id: str) -> None:
        from sqlalchemy import text
        with self._lock:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT 1 FROM pe_app_trust_scores WHERE app_id = :app_id"),
                    {"app_id": app_id},
                ).fetchone()
            if row is None:
                raise KeyError(f"App '{app_id}' not found in trust scores")

            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE pe_app_trust_scores SET "
                        "  score = 1.0, total_evaluations = 0, "
                        "  coercive_count = 0, normative_count = 0, permissive_count = 0, "
                        "  recent_events_json = '[]', last_updated_at = datetime('now') "
                        "WHERE app_id = :app_id"
                    ),
                    {"app_id": app_id},
                )
        logger.info(f"[DbAppTrustTracker] Reset trust score for app_id={app_id}")


# ══════════════════════════════════════════════════════════════════════════════
#  Legacy JSON-file implementation (fallback only)
# ══════════════════════════════════════════════════════════════════════════════

class AppTrustTracker:
    """
    DEPRECATED — Legacy implementation that persists scores to a JSON file.

    Used only as a fallback when AUDIT_SQLITE_PATH is not configured.
    Prefer DbAppTrustTracker for all new deployments.
    """

    def __init__(self, persist_path: str):
        self._path  = Path(persist_path)
        self._lock  = threading.RLock()
        self._state: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning(f"AppTrustTracker: could not load ({e}) — starting fresh")
            self._state = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"AppTrustTracker: failed to persist — {e}")

    def _ensure(self, app_id: str) -> None:
        if app_id not in self._state:
            self._state[app_id] = {
                "score": _CEILING, "events": [], "total_evaluations": 0,
                "verdict_counts": {"COERCIVE": 0, "NORMATIVE": 0, "PERMISSIVE": 0},
            }

    def record(self, app_id: str, verdict: str) -> float:
        verdict = verdict.upper()
        delta   = _DELTA.get(verdict, 0.0)
        with self._lock:
            self._ensure(app_id)
            entry     = self._state[app_id]
            old_score = entry["score"]
            new_score = round(max(_FLOOR, min(_CEILING, old_score + delta)), 4)
            entry["score"] = new_score
            entry["total_evaluations"] += 1
            entry["verdict_counts"][verdict] = entry["verdict_counts"].get(verdict, 0) + 1
            event = {"ts": datetime.now(tz=timezone.utc).isoformat(), "verdict": verdict, "delta": delta, "score": new_score}
            entry["events"].append(event)
            if len(entry["events"]) > _MAX_EVENTS:
                entry["events"] = entry["events"][-_MAX_EVENTS:]
            self._save()
        return new_score

    def get_score(self, app_id: str) -> Optional[float]:
        with self._lock:
            entry = self._state.get(app_id)
            return entry["score"] if entry else None

    def get_all_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                app_id: {
                    "app_id": app_id,
                    "score": e["score"],
                    "status": _score_status(e["score"]),
                    "total_evaluations": e.get("total_evaluations", 0),
                    "verdict_counts": dict(e.get("verdict_counts", {})),
                    "recent_events": list(e.get("events", []))[-10:],
                }
                for app_id, e in self._state.items()
            }

    # Compatibility shim for main.py reset endpoint (accesses _lock + _state + _save)
    @property
    def _legacy_state(self):
        return self._state
