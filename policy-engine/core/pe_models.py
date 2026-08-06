"""
Policy Engine — ORM models for PE-owned runtime tables.

These tables are written by the PE and read by both PE and PAP.
They mirror the definitions in pap/core/models.py for the same tables.

Using a separate PeBase (not imported from PAP) keeps the PE dependency-free
from PAP's code while still being able to call create_all() for these tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database import PeBase


def _now() -> datetime:
    return datetime.utcnow()


class PeSession(PeBase):
    """PE live session state — replaces sessions/<app_id>/<session_id>.json."""
    __tablename__ = "pe_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", "app_id", name="uq_pe_session"),
    )

    id:               Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id:       Mapped[str]   = mapped_column(String(128), nullable=False, index=True)
    app_id:           Mapped[str]   = mapped_column(String(128), nullable=False, index=True)
    state_json:       Mapped[dict]  = mapped_column(JSON, nullable=False)
    created_at_ts:    Mapped[float] = mapped_column(Float, nullable=False)
    last_activity_ts: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at:       Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class PeSessionSummary(PeBase):
    """PE compacted session summary — replaces summaries/<app_id>/<session_id>.json."""
    __tablename__ = "pe_session_summaries"
    __table_args__ = (
        UniqueConstraint("session_id", "app_id", name="uq_pe_summary"),
    )

    id:                Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id:        Mapped[str]   = mapped_column(String(128), nullable=False, index=True)
    app_id:            Mapped[str]   = mapped_column(String(128), nullable=False, index=True)
    summary_json:      Mapped[dict]  = mapped_column(JSON, nullable=False)
    end_reason:        Mapped[str]   = mapped_column(String(32), nullable=False)
    turn_count:        Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    coercive_count:    Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    final_trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    started_at_ts:     Mapped[float] = mapped_column(Float, nullable=False)
    ended_at_ts:       Mapped[float] = mapped_column(Float, nullable=False)
    created_at:        Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class PeAppTrustScore(PeBase):
    """PE per-app rolling trust score — replaces data/app_trust_scores.json."""
    __tablename__ = "pe_app_trust_scores"
    __table_args__ = (
        UniqueConstraint("app_id", name="uq_pe_trust_app"),
    )

    id:                 Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id:             Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    score:              Mapped[float]          = mapped_column(Float, nullable=False, default=1.0)
    total_evaluations:  Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    coercive_count:     Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    normative_count:    Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    permissive_count:   Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    recent_events_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    first_seen_at:      Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    last_updated_at:    Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)
