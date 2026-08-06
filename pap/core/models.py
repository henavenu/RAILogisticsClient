"""
PAP ORM models — RAI Trust Platform.

All tables use a two-prefix naming convention for SQLite/Postgres portability:
  config_*  — governed configuration (PAP owns writes, PE reads at startup)
  audit_*   — immutable event records (PE writes evals, PAP writes mutations)

21 entities
-----------
Config tier (PAP-owned):
  config_evaluation_dimensions   — master catalog of every scoreable dimension
  config_threshold_profiles      — named scoring bands (zero_tolerance, strict, standard)
  config_policy_packs            — pack definitions (mandatory, sanctions, …)
  config_pack_dimensions         — M2M: pack ↔ dimension with enforcement attrs
  config_apps                    — application registry
  config_app_pack_assignments    — M2M: app ↔ pack (explicit assignment required)
  config_dimension_overrides     — per-app tuning of a dimension within a pack
  config_policies                — per-dimension enforcement thresholds (PAP-edited)
  config_session_policy          — singleton: session trust delta & dedup config
  config_policy_templates        — named policy presets; seed library for operators

Audit tier:
  audit_pap_log                  — immutable record of every PAP mutation
  audit_evaluation_audit         — PE runtime evaluation decisions
  audit_evaluation_dimensions    — per-dimension scores for each evaluation
  audit_log                      — flat, denormalized export-friendly compliance copy

T2 proxy tier:
  tier2_eval_scores              — Phoenix/Arize annotation schema proxy;
                                   populated by MockTier2Provider (real providers via SPI)

T3 trust scoring tier:
  config_app_trust_weights       — per-app T3 sub-score weighting formula (PAP-editable)
  trust_score_snapshots          — time-bucketed composite trust scores (15-min compute job)
  trust_score_alerts             — SRE SLO burn-rate breach records
  agent_trust_scores             — rolling per-agent trust score (updated by PE on each eval)

COGS tier:
  config_model_pricing           — LLM pricing reference data (cost per 1M tokens)
  cogs_records                   — per-evaluation cost record (written by PE)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer,
    String, Text, UniqueConstraint, JSON,
)
from sqlalchemy.orm import mapped_column, Mapped

from core.database import Base


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.utcnow()


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG TIER
# ══════════════════════════════════════════════════════════════════════════════

class EvaluationDimension(Base):
    """
    Master catalog of every dimension the platform knows about.
    Adding a dimension here makes it available to packs — it does NOT activate
    evaluation for any app until the dimension is assigned to a pack AND that
    pack is assigned to the app.
    """
    __tablename__ = "config_evaluation_dimensions"

    dimension_id:     Mapped[str]           = mapped_column(String(64),  primary_key=True)
    category:         Mapped[str]           = mapped_column(String(32),  nullable=False, index=True)
    display_name:     Mapped[str]           = mapped_column(String(128), nullable=False)
    description:      Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    # gs_call | session_signal | local
    evaluator_type:   Mapped[str]           = mapped_column(String(32),  nullable=False, default="gs_call")
    # azure_content_safety | aws_bedrock_guardrail | embedding_filter | mock_provider | …
    provider:         Mapped[str]           = mapped_column(String(64),  nullable=False)
    # active | deferred | catalog_only
    status:           Mapped[str]           = mapped_column(String(16),  nullable=False, default="active")
    # JSON array of strings: ["user_prompt", "llm_response", …]
    supported_roles:  Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    requires_context: Mapped[bool]          = mapped_column(Boolean, default=False)
    # True = a single COERCIVE verdict immediately raises a POLICY_VIOLATION governance alert.
    # False = defer to the Sentinel window for cross-turn pattern analysis before alerting.
    # Configured per dimension; defaults False.  Set True for dimensions where one event
    # is already unambiguous (sanctions hits, credential exposure, active injection attacks).
    zero_tolerance:   Mapped[bool]          = mapped_column(Boolean, default=False, nullable=False)
    created_at:       Mapped[datetime]      = mapped_column(DateTime, default=_now)
    updated_at:       Mapped[datetime]      = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "dimension_id":    self.dimension_id,
            "category":        self.category,
            "display_name":    self.display_name,
            "description":     self.description,
            "evaluator_type":  self.evaluator_type,
            "provider":        self.provider,
            "status":          self.status,
            "supported_roles": self.supported_roles or [],
            "requires_context": self.requires_context,
            "zero_tolerance":  self.zero_tolerance,
            "created_at":      self.created_at.isoformat(),
            "updated_at":      self.updated_at.isoformat(),
        }


class ThresholdProfile(Base):
    """
    Named, reusable threshold bands.  Packs reference these by profile_id so
    operators tune scores by name, not by raw numbers.
    """
    __tablename__ = "config_threshold_profiles"

    profile_id:    Mapped[str]           = mapped_column(String(32), primary_key=True)
    permissive_lo: Mapped[float]         = mapped_column(Float, nullable=False)
    permissive_hi: Mapped[float]         = mapped_column(Float, nullable=False)
    normative_lo:  Mapped[float]         = mapped_column(Float, nullable=False)
    normative_hi:  Mapped[float]         = mapped_column(Float, nullable=False)
    coercive_lo:   Mapped[float]         = mapped_column(Float, nullable=False)
    coercive_hi:   Mapped[float]         = mapped_column(Float, nullable=False)
    description:   Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime]      = mapped_column(DateTime, default=_now)
    updated_at:    Mapped[datetime]      = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "profile_id":  self.profile_id,
            "thresholds": {
                "permissive": [self.permissive_lo, self.permissive_hi],
                "normative":  [self.normative_lo,  self.normative_hi],
                "coercive":   [self.coercive_lo,   self.coercive_hi],
            },
            "description": self.description,
        }


class PolicyPack(Base):
    """
    A named group of dimensions with shared enforcement rules.
    Mandatory packs (layer=mandatory) apply to every app automatically.
    All other packs require an explicit AppPackAssignment row.
    """
    __tablename__ = "config_policy_packs"

    pack_id:             Mapped[str]           = mapped_column(String(64),  primary_key=True)
    display_name:        Mapped[str]           = mapped_column(String(128), nullable=False)
    description:         Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    # mandatory | domain-addon | regulatory | platform-addon
    layer:               Mapped[str]           = mapped_column(String(32),  nullable=False)
    # always-on | flexible | flexible-threshold-only
    enforcement:         Mapped[str]           = mapped_column(String(32),  nullable=False)
    # abort-on-first-coercive | collect-all
    mode:                Mapped[str]           = mapped_column(String(32),  nullable=False, default="collect-all")
    combining_algorithm: Mapped[str]           = mapped_column(String(32),  nullable=False, default="worst_case")
    hitl_trigger:        Mapped[bool]          = mapped_column(Boolean, default=False)
    owner:               Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    version:             Mapped[str]           = mapped_column(String(16),  nullable=False, default="1.0.0")
    effective_date:      Mapped[Optional[str]] = mapped_column(String(16),  nullable=True)
    active:              Mapped[bool]          = mapped_column(Boolean, default=True)
    created_at:          Mapped[datetime]      = mapped_column(DateTime, default=_now)
    updated_at:          Mapped[datetime]      = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "pack_id":             self.pack_id,
            "display_name":        self.display_name,
            "description":         self.description,
            "layer":               self.layer,
            "enforcement":         self.enforcement,
            "mode":                self.mode,
            "combining_algorithm": self.combining_algorithm,
            "hitl_trigger":        self.hitl_trigger,
            "owner":               self.owner,
            "version":             self.version,
            "effective_date":      self.effective_date,
            "active":              self.active,
            "created_at":          self.created_at.isoformat(),
            "updated_at":          self.updated_at.isoformat(),
        }


class PackDimension(Base):
    """
    M2M join: which dimensions belong to which pack, with what enforcement attrs.
    FK to config_evaluation_dimensions enforces referential integrity —
    a pack cannot reference a dimension that does not exist in the catalog.
    """
    __tablename__ = "config_pack_dimensions"
    __table_args__ = (
        UniqueConstraint("pack_id", "dimension_id", name="uq_pack_dimension"),
    )

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    pack_id:      Mapped[str]           = mapped_column(String(64), ForeignKey("config_policy_packs.pack_id",    ondelete="CASCADE"), nullable=False, index=True)
    dimension_id: Mapped[str]           = mapped_column(String(64), ForeignKey("config_evaluation_dimensions.dimension_id", ondelete="RESTRICT"), nullable=False, index=True)
    profile_id:   Mapped[str]           = mapped_column(String(32), ForeignKey("config_threshold_profiles.profile_id",      ondelete="RESTRICT"), nullable=False)
    severity:     Mapped[int]           = mapped_column(Integer, nullable=False, default=2)  # 1=low 2=medium 3=high
    abort:        Mapped[bool]          = mapped_column(Boolean, default=False)
    # JSON array: roles active for this dimension within this pack
    active_roles: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    note:         Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime, default=_now)
    updated_at:   Mapped[datetime]      = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "pack_id":      self.pack_id,
            "dimension_id": self.dimension_id,
            "profile_id":   self.profile_id,
            "severity":     self.severity,
            "abort":        self.abort,
            "active_roles": self.active_roles or [],
            "note":         self.note,
        }


class App(Base):
    """
    Application registry.  Every app that submits evaluations must have a row here.
    Enforcement mode (BLOCK | REDACT | WARN) controls how COERCIVE verdicts are
    surfaced to the calling application.
    """
    __tablename__ = "config_apps"

    app_id:             Mapped[str]           = mapped_column(String(128), primary_key=True)
    display_name:       Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    description:        Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    # LOW | MEDIUM | HIGH
    base_tier:          Mapped[str]           = mapped_column(String(16),  nullable=False, default="MEDIUM")
    # BLOCK | REDACT | WARN
    enforcement_mode:   Mapped[str]           = mapped_column(String(16),  nullable=False, default="BLOCK")
    dynamic_evaluation: Mapped[bool]          = mapped_column(Boolean, default=False)
    owner:              Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    active:             Mapped[bool]          = mapped_column(Boolean, default=True)
    created_at:         Mapped[datetime]      = mapped_column(DateTime, default=_now)
    updated_at:         Mapped[datetime]      = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "app_id":             self.app_id,
            "display_name":       self.display_name,
            "description":        self.description,
            "base_tier":          self.base_tier,
            "enforcement_mode":   self.enforcement_mode,
            "dynamic_evaluation": self.dynamic_evaluation,
            "owner":              self.owner,
            "active":             self.active,
            "created_at":         self.created_at.isoformat(),
            "updated_at":         self.updated_at.isoformat(),
        }


class AppPackAssignment(Base):
    """
    Explicit assignment of a policy pack to an application.
    Mandatory packs are always active and do not need a row here.
    Non-mandatory packs are silently ignored for an app unless assigned.
    """
    __tablename__ = "config_app_pack_assignments"
    __table_args__ = (
        UniqueConstraint("app_id", "pack_id", name="uq_app_pack"),
    )

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id:      Mapped[str]      = mapped_column(String(128), ForeignKey("config_apps.app_id",         ondelete="CASCADE"),  nullable=False, index=True)
    pack_id:     Mapped[str]      = mapped_column(String(64),  ForeignKey("config_policy_packs.pack_id", ondelete="CASCADE"),  nullable=False, index=True)
    active:      Mapped[bool]     = mapped_column(Boolean, default=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    assigned_by: Mapped[str]      = mapped_column(String(128), nullable=False, default="pap-admin")

    def to_dict(self) -> dict:
        return {
            "app_id":      self.app_id,
            "pack_id":     self.pack_id,
            "active":      self.active,
            "assigned_at": self.assigned_at.isoformat(),
            "assigned_by": self.assigned_by,
        }


class DimensionOverride(Base):
    """
    Per-app tuning of a specific dimension within a specific pack.
    Overrides the pack-level profile/severity/abort for this app only.
    Setting active=False disables the dimension for this app without removing
    the pack assignment.
    """
    __tablename__ = "config_dimension_overrides"
    __table_args__ = (
        UniqueConstraint("app_id", "pack_id", "dimension_id", name="uq_dim_override"),
    )

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id:       Mapped[str]           = mapped_column(String(128), ForeignKey("config_apps.app_id",                         ondelete="CASCADE"),  nullable=False, index=True)
    pack_id:      Mapped[str]           = mapped_column(String(64),  ForeignKey("config_policy_packs.pack_id",                ondelete="CASCADE"),  nullable=False)
    dimension_id: Mapped[str]           = mapped_column(String(64),  ForeignKey("config_evaluation_dimensions.dimension_id",  ondelete="RESTRICT"), nullable=False)
    profile_id:   Mapped[Optional[str]] = mapped_column(String(32),  ForeignKey("config_threshold_profiles.profile_id",       ondelete="RESTRICT"), nullable=True)
    severity:     Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    abort:        Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    active:       Mapped[bool]          = mapped_column(Boolean, default=True)
    note:         Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime, default=_now)
    updated_at:   Mapped[datetime]      = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "app_id":       self.app_id,
            "pack_id":      self.pack_id,
            "dimension_id": self.dimension_id,
            "profile_id":   self.profile_id,
            "severity":     self.severity,
            "abort":        self.abort,
            "active":       self.active,
            "note":         self.note,
        }


class Policy(Base):
    """
    Per-dimension enforcement threshold rules.
    Global defaults have app_id=None.  App-specific rows override the global
    for that (dimension, app) pair (matched by PolicyRegistry lookup precedence).

    FK to config_evaluation_dimensions ensures every policy references a known
    dimension in the catalog.
    """
    __tablename__ = "config_policies"
    __table_args__ = (
        UniqueConstraint("dimension", "app_id", name="uq_policy_dim_app"),
    )

    id:        Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Column named 'dimension' for backward compat with policies.yaml and policy_sync
    dimension: Mapped[str]           = mapped_column(String(64), ForeignKey("config_evaluation_dimensions.dimension_id", ondelete="RESTRICT"), nullable=False, index=True)
    app_id:    Mapped[Optional[str]] = mapped_column(String(128), ForeignKey("config_apps.app_id", ondelete="CASCADE"),  nullable=True,  index=True)

    severity: Mapped[int] = mapped_column(Integer, default=1)  # 1=low 2=medium 3=high

    # Threshold bands (canonical score scale 1–9)
    permissive_lo: Mapped[float] = mapped_column(Float, default=1.0)
    permissive_hi: Mapped[float] = mapped_column(Float, default=4.0)
    normative_lo:  Mapped[float] = mapped_column(Float, default=5.0)
    normative_hi:  Mapped[float] = mapped_column(Float, default=6.0)
    coercive_lo:   Mapped[float] = mapped_column(Float, default=7.0)
    coercive_hi:   Mapped[float] = mapped_column(Float, default=9.0)

    remediation_normative: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    remediation_coercive:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # MessageWriter fields (migration 0017)
    policy_code:        Mapped[Optional[str]] = mapped_column(String(20),  nullable=True)
    normative_action:   Mapped[str]           = mapped_column(String(10),  default="REVISION")
    normative_guidance: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)

    # Remediation action fields
    human_note:          Mapped[Optional[str]]  = mapped_column(Text,       nullable=True)
    ops_alert:           Mapped[bool]           = mapped_column(Boolean,    default=False)
    incident_routing:    Mapped[str]            = mapped_column(String(32), default="none")
    # Structured machine-actionable instructions issued alongside remediation text.
    # Array of action objects, each with at minimum {"type": str, "priority": str}.
    # Supported types:
    #   hitl_platform — PE writes a platform_alerts row (category=HITL); PAP dashboard shows it
    #   hitl_app      — PE includes action in verdict response; SDK surfaces to app developer
    # Example:
    #   [{"type": "hitl_platform", "priority": "URGENT"},
    #    {"type": "hitl_app",      "priority": "HIGH"}]
    remediation_actions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Per-policy HITL: when True, a non-PERMISSIVE verdict on THIS policy raises a
    # human-in-the-loop interrupt (independent of any pack's hitl_trigger).
    hitl_trigger: Mapped[bool]               = mapped_column(Boolean,  default=False)

    # Lifecycle
    active:       Mapped[bool]               = mapped_column(Boolean,  default=True)
    version:      Mapped[int]               = mapped_column(Integer,  default=1)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:   Mapped[datetime]           = mapped_column(DateTime, default=_now)
    updated_at:   Mapped[datetime]           = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "id":        self.id,
            "dimension": self.dimension,
            "app_id":    self.app_id,
            "severity":  self.severity,
            "thresholds": {
                "permissive": [self.permissive_lo, self.permissive_hi],
                "normative":  [self.normative_lo,  self.normative_hi],
                "coercive":   [self.coercive_lo,   self.coercive_hi],
            },
            "remediations": {
                k: v for k, v in {
                    "normative": self.remediation_normative,
                    "coercive":  self.remediation_coercive,
                }.items() if v
            },
            "message_writer": {
                "policy_code":        self.policy_code,
                "normative_action":   self.normative_action or "REVISION",
                "normative_guidance": self.normative_guidance,
            },
            "actions": {
                "human_note":          self.human_note,
                "ops_alert":           self.ops_alert,
                "incident_routing":    self.incident_routing,
                "remediation_actions": self.remediation_actions or [],
            },
            "active":       self.active,
            "hitl_trigger": self.hitl_trigger,
            "version":      self.version,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "created_at":   self.created_at.isoformat(),
            "updated_at":   self.updated_at.isoformat(),
            "is_published": self.published_at is not None and self.published_at >= self.updated_at,
        }


class SessionPolicyConfig(Base):
    """
    Singleton row (id=1) holding session trust and deduplication parameters.
    Edited via PAP Session Policy tab; hot-reloaded into the Policy Engine.
    """
    __tablename__ = "config_session_policy"

    id:                         Mapped[int]   = mapped_column(Integer, primary_key=True, default=1)
    trust_delta_coercive:       Mapped[float] = mapped_column(Float, nullable=False, default=-0.20)
    trust_delta_normative:      Mapped[float] = mapped_column(Float, nullable=False, default=-0.05)
    trust_delta_permissive:     Mapped[float] = mapped_column(Float, nullable=False, default=0.02)
    trust_floor:                Mapped[float] = mapped_column(Float, nullable=False, default=0.20)
    trust_ceiling:              Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    session_ttl_seconds:        Mapped[int]   = mapped_column(Integer, nullable=False, default=1800)
    dedup_exact_block:          Mapped[bool]  = mapped_column(Boolean, nullable=False, default=True)
    dedup_similarity_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.92)
    dedup_window:               Mapped[int]   = mapped_column(Integer, nullable=False, default=50)
    dedup_repetition_penalty:   Mapped[float] = mapped_column(Float, nullable=False, default=-0.05)
    updated_at:                 Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    updated_by:                 Mapped[str]   = mapped_column(String(128), nullable=False, default="system")

    def to_dict(self) -> dict:
        return {
            "trust_deltas": {
                "COERCIVE":   self.trust_delta_coercive,
                "NORMATIVE":  self.trust_delta_normative,
                "PERMISSIVE": self.trust_delta_permissive,
            },
            "trust_floor":                self.trust_floor,
            "trust_ceiling":              self.trust_ceiling,
            "session_ttl_seconds":        self.session_ttl_seconds,
            "dedup": {
                "exact_block":          self.dedup_exact_block,
                "similarity_threshold": self.dedup_similarity_threshold,
                "window":               self.dedup_window,
                "repetition_penalty":   self.dedup_repetition_penalty,
            },
            "updated_at": self.updated_at.isoformat(),
            "updated_by": self.updated_by,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  AUDIT TIER
# ══════════════════════════════════════════════════════════════════════════════

class PapAuditLog(Base):
    """
    Immutable record of every PAP mutation.
    entity_type identifies what changed; entity_id is the primary key of that entity.
    old_values / new_values are JSON snapshots of the changed fields.
    """
    __tablename__ = "audit_pap_log"

    id:          Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    # policy | pack | dimension | app | assignment | override | session_policy | corporate_policy
    entity_type: Mapped[str]           = mapped_column(String(32),  nullable=False, index=True)
    entity_id:   Mapped[str]           = mapped_column(String(128), nullable=False, index=True)
    app_id:      Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # CREATE | UPDATE | DELETE | PUBLISH | ASSIGN | UNASSIGN | ENABLE | DISABLE
    action:      Mapped[str]           = mapped_column(String(16),  nullable=False, index=True)
    actor:       Mapped[str]           = mapped_column(String(128), nullable=False, default="pap-admin")
    old_values:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON
    new_values:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON
    notes:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp:   Mapped[datetime]      = mapped_column(DateTime, default=_now, index=True)

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "entity_type": self.entity_type,
            "entity_id":   self.entity_id,
            "app_id":      self.app_id,
            "action":      self.action,
            "actor":       self.actor,
            "old_values":  json.loads(self.old_values) if self.old_values else None,
            "new_values":  json.loads(self.new_values) if self.new_values else None,
            "notes":       self.notes,
            "timestamp":   self.timestamp.isoformat(),
        }


class EvaluationAudit(Base):
    """
    One row per PE evaluation call.  Written by the Policy Engine's
    SqliteAuditWriter (replaces/supplements the JSONL file for structured queries).
    The JSONL file remains for streaming / SSE use.
    """
    __tablename__ = "audit_evaluation_audit"

    id:                 Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    interaction_id:     Mapped[str]            = mapped_column(String(36),  nullable=False, unique=True, index=True)
    request_id:         Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    # Multi-tenancy scope — NULL in single-tenant deployments
    enterprise_id:      Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    app_id:             Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    session_id:         Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    agent_id:           Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    turn_number:        Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    role:               Mapped[str]            = mapped_column(String(32),  nullable=False)
    verdict:            Mapped[str]            = mapped_column(String(16),  nullable=False, index=True)
    violations:         Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    remediations:       Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    fail_reason:        Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)
    session_verdict:    Mapped[Optional[str]]  = mapped_column(String(16),  nullable=True)
    session_escalated:  Mapped[bool]           = mapped_column(Boolean, default=False)
    latency_ms:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    enforcement_action: Mapped[Optional[str]]  = mapped_column(String(16), nullable=True)
    # LLM model that produced the content being evaluated — required for COGS
    model_id:           Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    input_tokens:       Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    output_tokens:      Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    metadata_json:      Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    pack_trace:         Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    timestamp:          Mapped[datetime]       = mapped_column(DateTime, default=_now, index=True)

    def to_dict(self) -> dict:
        return {
            "interaction_id":    self.interaction_id,
            "request_id":        self.request_id,
            "app_id":            self.app_id,
            "session_id":        self.session_id,
            "agent_id":          self.agent_id,
            "turn_number":       self.turn_number,
            "role":              self.role,
            "verdict":           self.verdict,
            "violations":        self.violations or [],
            "remediations":      self.remediations or [],
            "fail_reason":       self.fail_reason,
            "session_verdict":   self.session_verdict,
            "session_escalated": self.session_escalated,
            "latency_ms":        self.latency_ms,
            "enforcement_action": self.enforcement_action,
            "timestamp":         self.timestamp.isoformat(),
        }


class EvaluationAuditDimension(Base):
    """
    Per-dimension score for a single evaluation audit row.
    dimension_id is a soft reference (no FK) — dimensions may be renamed or
    removed from the catalog over time but historical scores must be preserved.

    The (evaluation_audit_id, dimension_id) pair is UNIQUE so SqliteAuditWriter
    can use INSERT OR IGNORE for idempotent writes on PE retry.
    """
    __tablename__ = "audit_evaluation_dimensions"
    __table_args__ = (
        UniqueConstraint("evaluation_audit_id", "dimension_id", name="uq_dim_per_audit"),
    )

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_audit_id: Mapped[int]            = mapped_column(Integer, ForeignKey("audit_evaluation_audit.id", ondelete="CASCADE"), nullable=False, index=True)
    # Soft reference — no FK so historical scores survive dimension catalog changes
    dimension_id:        Mapped[str]            = mapped_column(String(64), nullable=False)
    score:               Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)   # raw 1–9 scale
    # (score - 1) / 8.0 → 0.0–1.0; standard for Phoenix/Arize annotation ingestion and T3 averaging
    score_normalized:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Explicit flag so we can store ALL evaluated dims (not just violations) and query cleanly
    is_violation:        Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    status:              Mapped[str]            = mapped_column(String(16), nullable=False)  # PASS | WARNING | FLAGGED
    provider:            Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)
    # Soft ref to config_policies.id — captures policy version active at eval time (regulatory proof)
    policy_id:           Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "dimension_id":    self.dimension_id,
            "score":           self.score,
            "score_normalized": self.score_normalized,
            "is_violation":    self.is_violation,
            "status":          self.status,
            "provider":        self.provider,
            "policy_id":       self.policy_id,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  AUDIT TIER — flat export copy
# ══════════════════════════════════════════════════════════════════════════════

class AuditLog(Base):
    """
    Flat, denormalized, export-friendly compliance record.
    One row per evaluation — completely self-contained, no joins required.

    Written by the PE simultaneously with audit_evaluation_audit.
    Intended consumers: Excel/CSV export, SIEM ingestion, regulatory audit.

    The normalised audit_evaluation_audit + audit_evaluation_dimensions tables
    are the source of truth for application queries.  This table is the
    compliance projection of the same data in export-ready form.
    """
    __tablename__ = "audit_log"

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Correlation — same values as audit_evaluation_audit
    interaction_id:      Mapped[str]            = mapped_column(String(36),  nullable=False, unique=True, index=True)
    request_id:          Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    enterprise_id:       Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    app_id:              Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    agent_id:            Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    session_id:          Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    turn_number:         Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)

    # Evaluation context
    role:                Mapped[str]            = mapped_column(String(32),  nullable=False)
    model_id:            Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)

    # Verdict
    verdict:             Mapped[str]            = mapped_column(String(16),  nullable=False, index=True)
    session_verdict:     Mapped[Optional[str]]  = mapped_column(String(16),  nullable=True)
    session_escalated:   Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    enforcement_action:  Mapped[Optional[str]]  = mapped_column(String(16),  nullable=True)

    # Dimension detail — flattened as JSON so export needs no join
    # Format: [{"dimension_id": "hate", "score": 2, "score_normalized": 0.125,
    #            "is_violation": false, "status": "PASS", "provider": "mock", "policy_id": 3}]
    dimensions_json:     Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    violations_json:     Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    remediations_json:   Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Performance
    latency_ms:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # COGS (pre-computed at write time — export needs no pricing join)
    input_tokens:        Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    output_tokens:       Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    estimated_cost_usd:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    fail_reason:         Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)
    timestamp:           Mapped[datetime]       = mapped_column(DateTime, nullable=False, index=True)
    exported_at:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "interaction_id":    self.interaction_id,
            "request_id":        self.request_id,
            "enterprise_id":     self.enterprise_id,
            "app_id":            self.app_id,
            "agent_id":          self.agent_id,
            "session_id":        self.session_id,
            "turn_number":       self.turn_number,
            "role":              self.role,
            "model_id":          self.model_id,
            "verdict":           self.verdict,
            "session_verdict":   self.session_verdict,
            "session_escalated": self.session_escalated,
            "enforcement_action": self.enforcement_action,
            "dimensions":        self.dimensions_json or [],
            "violations":        self.violations_json or [],
            "remediations":      self.remediations_json or [],
            "latency_ms":        self.latency_ms,
            "input_tokens":      self.input_tokens,
            "output_tokens":     self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "fail_reason":       self.fail_reason,
            "timestamp":         self.timestamp.isoformat(),
            "exported_at":       self.exported_at.isoformat() if self.exported_at else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  T2 PROXY TIER
# ══════════════════════════════════════════════════════════════════════════════

class Tier2EvalScore(Base):
    """
    Phoenix/Arize annotation schema proxy for Tier 2 evaluation scores.

    Field names and semantics mirror the Phoenix/Arize annotation API so the
    TrustDataProvider SPI can read from this table identically regardless of
    which observability backend is active.

    MockTier2Provider populates this with seeded data.
    A real PhoenixProvider or ArizeProvider reads from the live observability
    store instead and may optionally sync results back here for caching.
    """
    __tablename__ = "tier2_eval_scores"

    id:             Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Correlation to T1 evaluation
    interaction_id: Mapped[str]            = mapped_column(String(36),  nullable=False, index=True)
    app_id:         Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    enterprise_id:  Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    agent_id:       Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)

    # Phoenix/Arize annotation fields (names match their API)
    eval_name:      Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    score:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # 0.0–1.0
    label:          Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)  # PASS | FAIL | NEEDS_REVIEW
    explanation:    Mapped[Optional[str]]  = mapped_column(Text, nullable=True)

    # Who/what produced this score
    # LLM = scored by an LLM judge | HUMAN = human annotator | CODE = deterministic rule
    annotator_kind: Mapped[str]            = mapped_column(String(32), nullable=False, default="CODE")
    provider_id:    Mapped[str]            = mapped_column(String(64), nullable=False, default="mock")

    evaluated_at:   Mapped[datetime]       = mapped_column(DateTime, nullable=False, index=True)
    created_at:     Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {
            "interaction_id": self.interaction_id,
            "app_id":         self.app_id,
            "eval_name":      self.eval_name,
            "score":          self.score,
            "label":          self.label,
            "explanation":    self.explanation,
            "annotator_kind": self.annotator_kind,
            "provider_id":    self.provider_id,
            "evaluated_at":   self.evaluated_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  T3 TRUST SCORING TIER
# ══════════════════════════════════════════════════════════════════════════════

class AppTrustWeights(Base):
    """
    Per-app T3 sub-score weighting formula.
    Operators edit this via the PAP UI. Weights must sum to 1.0.
    NULL app_id = enterprise-wide default (used when no app-specific row exists).
    """
    __tablename__ = "config_app_trust_weights"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "app_id", name="uq_trust_weights_app"),
    )

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    enterprise_id:       Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    app_id:              Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)  # NULL = enterprise default

    # Sub-score weights (must sum to 1.0)
    weight_safety:       Mapped[float]          = mapped_column(Float, nullable=False, default=0.30)
    weight_security:     Mapped[float]          = mapped_column(Float, nullable=False, default=0.25)
    weight_compliance:   Mapped[float]          = mapped_column(Float, nullable=False, default=0.20)
    weight_operational:  Mapped[float]          = mapped_column(Float, nullable=False, default=0.10)
    weight_quality:      Mapped[float]          = mapped_column(Float, nullable=False, default=0.15)

    # Burn rate alert thresholds — max score drop before alert fires
    alert_threshold_1h:  Mapped[float]          = mapped_column(Float, nullable=False, default=0.05)
    alert_threshold_6h:  Mapped[float]          = mapped_column(Float, nullable=False, default=0.10)
    alert_threshold_24h: Mapped[float]          = mapped_column(Float, nullable=False, default=0.15)

    active:              Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    created_at:          Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    updated_at:          Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "enterprise_id": self.enterprise_id,
            "app_id":        self.app_id,
            "weights": {
                "safety":      self.weight_safety,
                "security":    self.weight_security,
                "compliance":  self.weight_compliance,
                "operational": self.weight_operational,
                "quality":     self.weight_quality,
            },
            "alert_thresholds": {
                "1h":  self.alert_threshold_1h,
                "6h":  self.alert_threshold_6h,
                "24h": self.alert_threshold_24h,
            },
        }


class MetricSnapshot(Base):
    """
    Generic time-series metric store — Prometheus recording-rule pattern.

    Every platform metric (trust score, PII exposure rate, latency p95, token counts,
    violation rates) is stored here as a name + label set + value row.  This avoids
    per-metric table sprawl: any new metric is a new metric_name value, not a new table.

    Label set mirrors the Prometheus label model — a JSON object of arbitrary
    key-value pairs that dimension the metric:
        {"app_id": "mybank", "role": "tool_call", "eval_mode": "SYNC"}

    Common metric_name values:
        app_trust_score          — composite trust score (0–100)
        pii_exposure_rate        — PII-flagged evals / total evals
        latency_p95_ms           — p95 governance latency (SYNC evals only)
        violation_rate           — violations / total evals
        token_count_prompt       — total prompt tokens in window
        token_count_completion   — total completion tokens in window

    window_type:
        point  — instantaneous reading
        1h | 6h | 24h | 7d | 30d  — aggregate over the window

    Long-term: read path swaps to Arize GraphQL Metrics API via TrustDataProvider SPI.
    This table is the local dev / Phoenix fallback.
    """
    __tablename__ = "metric_snapshots"

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_name:  Mapped[str]      = mapped_column(String(128), nullable=False)
    labels:       Mapped[dict]     = mapped_column(JSON, nullable=False, default=dict)
    value:        Mapped[float]    = mapped_column(Float, nullable=False)
    window_type:  Mapped[str]      = mapped_column(String(8), nullable=False, default="point")
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    computed_at:  Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {
            "metric_name":  self.metric_name,
            "labels":       self.labels,
            "value":        self.value,
            "window_type":  self.window_type,
            "window_start": self.window_start.isoformat(),
            "computed_at":  self.computed_at.isoformat(),
        }


# ── Kept: TrustScoreAlert — events not metrics, need resolution lifecycle ────

class TrustScoreSnapshot(Base):
    """
    DEPRECATED — replaced by MetricSnapshot. Retained for downgrade compatibility only.
    Do not use in new code.
    """
    __tablename__ = "trust_score_snapshots"

    id:                Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    enterprise_id:     Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    app_id:            Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)  # NULL = enterprise roll-up
    agent_id:          Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)  # NULL = app-level

    # Time window
    window_type:       Mapped[str]            = mapped_column(String(8),  nullable=False)  # 1h | 6h | 24h | 7d | 30d
    window_start:      Mapped[datetime]       = mapped_column(DateTime, nullable=False)
    window_end:        Mapped[datetime]       = mapped_column(DateTime, nullable=False)

    # Composite score (weighted average using config_app_trust_weights)
    trust_score:       Mapped[float]          = mapped_column(Float, nullable=False)

    # Sub-scores (0.0–1.0; NULL if no evaluations for that category in window)
    score_safety:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_security:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_compliance:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_operational: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_quality:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Volume
    total_evaluations: Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    total_violations:  Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    violation_rate:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Burn rate (SRE SLO style)
    # score_delta  — trust_score minus previous window of same type
    # burn_rate    — score_delta normalised per hour (negative = degrading)
    score_delta:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    burn_rate:         Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    computed_at:       Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, index=True)

    def to_dict(self) -> dict:
        return {
            "enterprise_id":     self.enterprise_id,
            "app_id":            self.app_id,
            "agent_id":          self.agent_id,
            "window_type":       self.window_type,
            "window_start":      self.window_start.isoformat(),
            "window_end":        self.window_end.isoformat(),
            "trust_score":       self.trust_score,
            "sub_scores": {
                "safety":      self.score_safety,
                "security":    self.score_security,
                "compliance":  self.score_compliance,
                "operational": self.score_operational,
                "quality":     self.score_quality,
            },
            "total_evaluations": self.total_evaluations,
            "total_violations":  self.total_violations,
            "violation_rate":    self.violation_rate,
            "score_delta":       self.score_delta,
            "burn_rate":         self.burn_rate,
            "computed_at":       self.computed_at.isoformat(),
        }


class TrustScoreAlert(Base):
    """
    SRE SLO burn-rate breach record.

    Triggered when both the fast window (1h) AND medium window (6h) burn rates
    exceed their configured thresholds simultaneously — same two-window condition
    as Google SRE error budget burn rate alerts.

    alert_type values:
      burn_rate_exceeded  — multi-window burn rate threshold breached
      sustained_low       — trust_score below floor for N consecutive windows
      dimension_spike     — single dimension violation rate jumped sharply
    """
    __tablename__ = "trust_score_alerts"

    id:                   Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    enterprise_id:        Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    app_id:               Mapped[Optional[str]]  = mapped_column(String(128), nullable=True, index=True)
    agent_id:             Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)

    alert_type:           Mapped[str]            = mapped_column(String(32), nullable=False)
    # LOW | MEDIUM | HIGH | CRITICAL
    severity:             Mapped[str]            = mapped_column(String(16), nullable=False)

    # Burn rate detail (populated for burn_rate_exceeded alerts)
    fast_window:          Mapped[Optional[str]]  = mapped_column(String(8),  nullable=True)  # '1h'
    medium_window:        Mapped[Optional[str]]  = mapped_column(String(8),  nullable=True)  # '6h'
    fast_burn_rate:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    medium_burn_rate:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trust_score_at_alert: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Dimension that triggered (for dimension_spike alerts)
    dimension_id:         Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)

    # Resolution lifecycle: open | acknowledged | resolved | suppressed
    status:               Mapped[str]            = mapped_column(String(16), nullable=False, default="open", index=True)
    triggered_at:         Mapped[datetime]       = mapped_column(DateTime, nullable=False, index=True)
    acknowledged_at:      Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes:                Mapped[Optional[str]]  = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "enterprise_id":        self.enterprise_id,
            "app_id":               self.app_id,
            "agent_id":             self.agent_id,
            "alert_type":           self.alert_type,
            "severity":             self.severity,
            "fast_window":          self.fast_window,
            "medium_window":        self.medium_window,
            "fast_burn_rate":       self.fast_burn_rate,
            "medium_burn_rate":     self.medium_burn_rate,
            "trust_score_at_alert": self.trust_score_at_alert,
            "dimension_id":         self.dimension_id,
            "status":               self.status,
            "triggered_at":         self.triggered_at.isoformat(),
            "acknowledged_at":      self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "resolved_at":          self.resolved_at.isoformat() if self.resolved_at else None,
            "notes":                self.notes,
        }


class AgentTrustScore(Base):
    """
    DEPRECATED — replaced by MetricSnapshot with labels={agent_id: ...}.
    Retained for downgrade compatibility only. Do not use in new code.
    """
    __tablename__ = "agent_trust_scores"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "app_id", "agent_id", name="uq_agent_trust"),
    )

    id:                 Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    enterprise_id:      Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    app_id:             Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    agent_id:           Mapped[str]            = mapped_column(String(128), nullable=False, index=True)

    # Rolling trust score (0.0–1.0, same delta mechanics as session trust)
    trust_score:        Mapped[float]          = mapped_column(Float, nullable=False, default=1.0)

    # Category sub-scores (rolling averages)
    score_safety:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_security:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_compliance:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    total_evaluations:  Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    total_violations:   Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    last_violation_at:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    first_seen_at:      Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    last_updated_at:    Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "enterprise_id":     self.enterprise_id,
            "app_id":            self.app_id,
            "agent_id":          self.agent_id,
            "trust_score":       self.trust_score,
            "sub_scores": {
                "safety":     self.score_safety,
                "security":   self.score_security,
                "compliance": self.score_compliance,
            },
            "total_evaluations": self.total_evaluations,
            "total_violations":  self.total_violations,
            "last_violation_at": self.last_violation_at.isoformat() if self.last_violation_at else None,
            "first_seen_at":     self.first_seen_at.isoformat(),
            "last_updated_at":   self.last_updated_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  COGS TIER
# ══════════════════════════════════════════════════════════════════════════════

class ModelPricing(Base):
    """
    LLM pricing reference data — cost per 1 million tokens (industry standard unit).
    Seeded at startup; operators update via PAP UI when providers change pricing.
    effective_until=NULL means this is the currently active price for that model.
    """
    __tablename__ = "config_model_pricing"
    __table_args__ = (
        UniqueConstraint("provider", "model_id", "effective_from", name="uq_model_pricing"),
    )

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider:            Mapped[str]            = mapped_column(String(64),  nullable=False)   # openai | anthropic | groq | azure | aws
    model_id:            Mapped[str]            = mapped_column(String(128), nullable=False, index=True)

    # Cost in USD per 1 million tokens
    cost_per_1m_input:   Mapped[float]          = mapped_column(Float, nullable=False)
    cost_per_1m_output:  Mapped[float]          = mapped_column(Float, nullable=False)

    # Validity window — prices change over time
    effective_from:      Mapped[datetime]       = mapped_column(DateTime, nullable=False)
    effective_until:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # NULL = current price

    active:              Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    created_at:          Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {
            "provider":           self.provider,
            "model_id":           self.model_id,
            "cost_per_1m_input":  self.cost_per_1m_input,
            "cost_per_1m_output": self.cost_per_1m_output,
            "effective_from":     self.effective_from.isoformat(),
            "effective_until":    self.effective_until.isoformat() if self.effective_until else None,
            "active":             self.active,
        }


class CogsRecord(Base):
    """
    DEPRECATED — COGS is now computed on demand by the Dashboard COGS API,
    which queries Phoenix span attributes (llm.token_count.*) and applies
    config_model_pricing at query time. No per-evaluation cost records needed.
    Retained for downgrade compatibility only. Do not use in new code.
    """
    __tablename__ = "cogs_records"

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Correlation
    interaction_id:      Mapped[str]            = mapped_column(String(36),  nullable=False, index=True)
    app_id:              Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    enterprise_id:       Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)

    # LLM call details
    provider:            Mapped[str]            = mapped_column(String(64),  nullable=False)
    model_id:            Mapped[str]            = mapped_column(String(128), nullable=False)
    input_tokens:        Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    output_tokens:       Mapped[int]            = mapped_column(Integer, nullable=False, default=0)

    # Price snapshot at evaluation time (denormalized — survives pricing table updates)
    cost_per_1m_input:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_per_1m_output:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_cost_usd:      Mapped[float]           = mapped_column(Float, nullable=False, default=0.0)

    timestamp:           Mapped[datetime]        = mapped_column(DateTime, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "interaction_id":     self.interaction_id,
            "app_id":             self.app_id,
            "enterprise_id":      self.enterprise_id,
            "provider":           self.provider,
            "model_id":           self.model_id,
            "input_tokens":       self.input_tokens,
            "output_tokens":      self.output_tokens,
            "cost_per_1m_input":  self.cost_per_1m_input,
            "cost_per_1m_output": self.cost_per_1m_output,
            "total_cost_usd":     self.total_cost_usd,
            "timestamp":          self.timestamp.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG TIER — additions
# ══════════════════════════════════════════════════════════════════════════════

class PolicyTemplate(Base):
    """
    Named, versioned policy preset that operators can apply to an app in one click.
    Seed templates (is_seed=True) ship with the platform.
    Operators can create custom templates (is_seed=False) from the PAP UI.

    dimension_policies — JSON list of policy settings to apply:
        [{"dimension": "hate", "thresholds": {"permissive": [1,4], ...}, "severity": 2}, ...]
    pack_ids — JSON list of pack_ids this template activates on the target app.
    """
    __tablename__ = "config_policy_templates"

    id:                Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id:       Mapped[str]            = mapped_column(String(64),  nullable=False, unique=True)
    display_name:      Mapped[str]            = mapped_column(String(128), nullable=False)
    description:       Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    # financial_services | healthcare | general | ...
    industry:          Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)
    # LOW | MEDIUM | HIGH
    risk_tier:         Mapped[Optional[str]]  = mapped_column(String(16),  nullable=True)

    # Template content
    dimension_policies: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    pack_ids:           Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    is_seed:           Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    active:            Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    version:           Mapped[str]            = mapped_column(String(16), nullable=False, default="1.0.0")
    created_at:        Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    updated_at:        Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "template_id":        self.template_id,
            "display_name":       self.display_name,
            "description":        self.description,
            "industry":           self.industry,
            "risk_tier":          self.risk_tier,
            "dimension_policies": self.dimension_policies or [],
            "pack_ids":           self.pack_ids or [],
            "is_seed":            self.is_seed,
            "active":             self.active,
            "version":            self.version,
            "created_at":         self.created_at.isoformat(),
            "updated_at":         self.updated_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED SERVICE CONFIG TIER — replaces YAML/JSON file stores
#  Written at seed time from YAML; authoritative source for all runtime reads.
# ══════════════════════════════════════════════════════════════════════════════

class ServiceConfig(Base):
    """
    Generic JSON configuration blob store.

    Replaces static YAML/JSON files that PE and GS previously read at startup:
      ('pe', 'threshold_profiles')  — threshold_profiles.yaml (full, incl. step_map)
      ('pe', 'context_routing')     — context_routing.yaml
      ('gs', 'mock_scenarios')      — mock_scenarios.yaml
      ('gs', 'embedding_filter')    — embedding_filter.yaml
      ('gs', 'providers')           — providers.yaml (no secrets — env-var refs only)

    Seeded once from YAML source files.  Operator edits via PAP UI update this row;
    the YAML file is never read again after the initial seed.
    """
    __tablename__ = "config_service_configs"
    __table_args__ = (
        UniqueConstraint("service_name", "config_key", name="uq_service_config"),
    )

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    # "pe" | "gs"
    service_name: Mapped[str]           = mapped_column(String(32), nullable=False, index=True)
    # e.g. "threshold_profiles" | "context_routing" | "mock_scenarios"
    config_key:   Mapped[str]           = mapped_column(String(64), nullable=False)
    config_json:  Mapped[dict]          = mapped_column(JSON, nullable=False)
    seeded_from:  Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime, nullable=False, default=_now)
    updated_at:   Mapped[datetime]      = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "service_name": self.service_name,
            "config_key":   self.config_key,
            "config_json":  self.config_json,
            "updated_at":   self.updated_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  POLICY ENGINE RUNTIME TIER — replaces JSON/disk session and trust stores
# ══════════════════════════════════════════════════════════════════════════════

class PeSession(Base):
    """
    Policy Engine live session state.

    Replaces sessions/<app_id>/<session_id>.json on disk.
    Written by the PE SqliteSessionStore on every evaluation turn.
    Read by PAP dashboard for active-session observability.
    """
    __tablename__ = "pe_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", "app_id", name="uq_pe_session"),
    )

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id:    Mapped[str]      = mapped_column(String(128), nullable=False, index=True)
    app_id:        Mapped[str]      = mapped_column(String(128), nullable=False, index=True)
    # Full SessionState dataclass serialised to JSON
    state_json:    Mapped[dict]     = mapped_column(JSON, nullable=False)
    # Unix epoch floats for fast TTL checks (avoid JSON parse on hot path)
    created_at_ts: Mapped[float]    = mapped_column(Float, nullable=False)
    last_activity_ts: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at:    Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "session_id":       self.session_id,
            "app_id":           self.app_id,
            "state":            self.state_json,
            "created_at_ts":    self.created_at_ts,
            "last_activity_ts": self.last_activity_ts,
            "updated_at":       self.updated_at.isoformat(),
        }


class PeSessionSummary(Base):
    """
    Policy Engine compacted session summary.

    Replaces summaries/<app_id>/<session_id>.json on disk.
    Written by SqliteSessionStore on session expiry / termination.
    Permanent record — never updated after creation.
    """
    __tablename__ = "pe_session_summaries"
    __table_args__ = (
        UniqueConstraint("session_id", "app_id", name="uq_pe_summary"),
    )

    id:           Mapped[int]    = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id:   Mapped[str]    = mapped_column(String(128), nullable=False, index=True)
    app_id:       Mapped[str]    = mapped_column(String(128), nullable=False, index=True)
    # Full SessionSummary dataclass serialised to JSON
    summary_json: Mapped[dict]   = mapped_column(JSON, nullable=False)
    # Extracted fields for dashboard queries (avoid full JSON parse)
    end_reason:   Mapped[str]    = mapped_column(String(32),  nullable=False)
    turn_count:   Mapped[int]    = mapped_column(Integer, nullable=False, default=0)
    coercive_count: Mapped[int]  = mapped_column(Integer, nullable=False, default=0)
    final_trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    started_at_ts: Mapped[float] = mapped_column(Float, nullable=False)
    ended_at_ts:   Mapped[float] = mapped_column(Float, nullable=False)
    created_at:   Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    def to_dict(self) -> dict:
        return {
            "session_id":        self.session_id,
            "app_id":            self.app_id,
            "summary":           self.summary_json,
            "end_reason":        self.end_reason,
            "turn_count":        self.turn_count,
            "coercive_count":    self.coercive_count,
            "final_trust_score": self.final_trust_score,
            "started_at_ts":     self.started_at_ts,
            "ended_at_ts":       self.ended_at_ts,
            "created_at":        self.created_at.isoformat(),
        }


class PeAppTrustScore(Base):
    """
    Policy Engine per-application rolling trust score.

    Replaces data/app_trust_scores.json on disk.
    Written by DbAppTrustTracker on every evaluation.
    Read by PAP dashboard for trust-score visualisation.
    """
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
    # Last 50 verdict events as JSON list for dashboard sparkline
    recent_events_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    first_seen_at:      Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    last_updated_at:    Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "app_id":            self.app_id,
            "score":             self.score,
            "total_evaluations": self.total_evaluations,
            "verdict_counts": {
                "COERCIVE":   self.coercive_count,
                "NORMATIVE":  self.normative_count,
                "PERMISSIVE": self.permissive_count,
            },
            "recent_events":  self.recent_events_json or [],
            "first_seen_at":  self.first_seen_at.isoformat(),
            "last_updated_at": self.last_updated_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM ALERTS TIER — operational and HITL alert store
# ══════════════════════════════════════════════════════════════════════════════

class PlatformAlert(Base):
    """
    Unified alert store for HITL review items and operational alerts.

    category values:
      HITL                — policy pack with hitl_trigger=true fired; needs governance review
      PROVIDER_UNAVAILABLE — external provider failed; dims returned PASS by fail-open
      THRESHOLD_BREACH    — a trust score dropped below a configured alert threshold
      EVAL_ANOMALY        — unexpected pattern in evaluation results

    HITL alerts are created by the PE when a pack with hitl_trigger=true fires,
    or when a policy has a remediation_action of type 'hitl_platform'.

    Provider alerts are created by the GS base provider when _emit_provider_alert()
    is called from the fail-open wrapper.

    The PAP dashboard renders category-filtered views of this table.
    SSE stream pushes new rows to connected dashboard clients in real time.
    """
    __tablename__ = "platform_alerts"

    id:             Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)

    # HITL | PROVIDER_UNAVAILABLE | THRESHOLD_BREACH | EVAL_ANOMALY
    category:       Mapped[str]            = mapped_column(String(32),  nullable=False, index=True)
    # HIGH | URGENT | MEDIUM | INFO
    severity:       Mapped[str]            = mapped_column(String(16),  nullable=False, default="HIGH")

    # Source context — all nullable; not every alert has all fields
    app_id:         Mapped[Optional[str]]  = mapped_column(String(128), nullable=True,  index=True)
    interaction_id: Mapped[Optional[str]]  = mapped_column(String(128), nullable=True,  index=True)
    pack_id:        Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)
    dimension:      Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)
    provider_name:  Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)

    # Full structured context for the reviewer
    payload:        Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Lifecycle
    triggered_at:   Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, index=True)
    resolved_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by:    Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    # acknowledged | override | escalated | auto_resolved
    resolution:     Mapped[Optional[str]]  = mapped_column(String(32),  nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            "category":       self.category,
            "severity":       self.severity,
            "app_id":         self.app_id,
            "interaction_id": self.interaction_id,
            "pack_id":        self.pack_id,
            "dimension":      self.dimension,
            "provider_name":  self.provider_name,
            "payload":        self.payload or {},
            "triggered_at":   self.triggered_at.isoformat(),
            "resolved_at":    self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by":    self.resolved_by,
            "resolution":     self.resolution,
            "resolution_note": self.resolution_note,
            "is_open":        self.resolved_at is None,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  GOVERNANCE ALERTS TIER — unified, tiered alert store
# ══════════════════════════════════════════════════════════════════════════════

class GovernanceAlert(Base):
    """
    Unified, tiered governance alert store.

    Replaces the two-table (platform_alerts + trust_score_alerts) approach.
    New alerts written here exclusively from migration 0015 onward.

    Tier values
    ───────────
      INCIDENT        Multi-turn / compound pattern actively unfolding
                      Source: Sentinel ALERT → Coordinator Tier 1 or 2
      HITL            Human decision gate — blocks or requires review
                      Source: PE HITL pack trigger, Coordinator Tier 1
      POLICY_VIOLATION Single evaluation threshold breach (zero-tolerance dims)
                      Source: PE COERCIVE verdict on zero-tolerance dimension
      RELIABILITY     Platform health — governance quality degraded
                      Source: SRE burn-rate breach, provider unavailable
      ADVISORY        Pattern detected, tuning recommended, no immediate action
                      Source: Sentinel WATCH, Coordinator Tier 3/4

    Two-source model
    ────────────────
      Primary:    Sentinel duo raises INCIDENT / ADVISORY from cross-turn patterns.
      Safety net: PE raises POLICY_VIOLATION immediately on COERCIVE verdict for
                  any zero-tolerance dimension (no Sentinel window needed).

    Deduplication
    ─────────────
      One POLICY_VIOLATION per (session_id, dimension). Subsequent hits increment
      recurrence_count and update last_seen_at rather than inserting a new row.
    """
    __tablename__ = "governance_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Classification ────────────────────────────────────────────────────────
    # INCIDENT | HITL | POLICY_VIOLATION | RELIABILITY | ADVISORY
    tier:     Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # URGENT | HIGH | MEDIUM | LOW
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="HIGH")

    # ── Actor attribution ─────────────────────────────────────────────────────
    # sentinel | policy_engine | sre_metrics | guardrail_service | coordinator
    raiser:        Mapped[str]           = mapped_column(String(64),  nullable=False)
    # e.g. "sentinel_pattern:dimensional_escalation", "pe_pack:mandatory_safety"
    raiser_detail: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # user | agent | session | system
    caller_type:   Mapped[Optional[str]] = mapped_column(String(32),  nullable=True)
    # session_id / agent_name / app_id / provider_name
    caller_id:     Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    app_id:        Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    # ── Reason & condition ────────────────────────────────────────────────────
    # dimensional_escalation | burn_rate_breach | hitl_trigger |
    # coercive_verdict | structuring | provider_unavailable | ...
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # One-sentence human-readable reason
    reason_text: Mapped[Optional[str]] = mapped_column(Text,       nullable=True)
    # The exact signal that fired: "sensitive_data_disclosure score=8 ≥ threshold 7"
    condition:   Mapped[Optional[str]] = mapped_column(Text,       nullable=True)
    # Which RAI dimension triggered this alert
    dimension:   Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Policy pack that fired
    pack_id:     Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── Evidence links ────────────────────────────────────────────────────────
    interaction_id: Mapped[Optional[str]]  = mapped_column(String(256), nullable=True, index=True)
    session_id:     Mapped[Optional[str]]  = mapped_column(String(256), nullable=True, index=True)
    # [] of turn numbers where key violations occurred (from Sentinel)
    evidence_turns: Mapped[Optional[list]] = mapped_column(JSON,        nullable=True)

    # ── Coordinator recommendation ─────────────────────────────────────────────
    # Written asynchronously once Coordinator analysis completes.
    # Null until Coordinator enrichment fires (provisional alert state).
    coord_action: Mapped[Optional[str]]  = mapped_column(Text,       nullable=True)
    # OPERATOR | COMPLIANCE | MLRO | SYSTEM | AGENT_TEAM | ML_OPS
    coord_target: Mapped[Optional[str]]  = mapped_column(String(64), nullable=True)
    # {type, urgency, assignment_group, short_description}
    coord_itsm:   Mapped[Optional[dict]] = mapped_column(JSON,       nullable=True)

    # ── Deduplication ─────────────────────────────────────────────────────────
    recurrence_count: Mapped[int]            = mapped_column(Integer,  nullable=False, default=1)
    last_seen_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Payload — structured context for the reviewer ─────────────────────────
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    # open | acknowledged | in_review | resolved | suppressed | false_positive
    status:           Mapped[str]               = mapped_column(String(32),  nullable=False, default="open", index=True)
    triggered_at:     Mapped[datetime]          = mapped_column(DateTime,    nullable=False, default=_now, index=True)
    acknowledged_at:  Mapped[Optional[datetime]] = mapped_column(DateTime,   nullable=True)
    acknowledged_by:  Mapped[Optional[str]]     = mapped_column(String(128), nullable=True)
    resolved_at:      Mapped[Optional[datetime]] = mapped_column(DateTime,   nullable=True)
    resolved_by:      Mapped[Optional[str]]     = mapped_column(String(128), nullable=True)
    # acknowledged | override | escalated | auto_resolved | false_positive
    resolution:       Mapped[Optional[str]]     = mapped_column(String(64),  nullable=True)
    resolution_note:  Mapped[Optional[str]]     = mapped_column(Text,        nullable=True)
    created_at:       Mapped[Optional[datetime]] = mapped_column(DateTime,   nullable=True, default=_now)
    updated_at:       Mapped[Optional[datetime]] = mapped_column(DateTime,   nullable=True, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "tier":             self.tier,
            "severity":         self.severity,
            "raiser":           self.raiser,
            "raiser_detail":    self.raiser_detail,
            "caller_type":      self.caller_type,
            "caller_id":        self.caller_id,
            "app_id":           self.app_id,
            "reason_code":      self.reason_code,
            "reason_text":      self.reason_text,
            "condition":        self.condition,
            "dimension":        self.dimension,
            "pack_id":          self.pack_id,
            "interaction_id":   self.interaction_id,
            "session_id":       self.session_id,
            "evidence_turns":   self.evidence_turns or [],
            "coord_action":     self.coord_action,
            "coord_target":     self.coord_target,
            "coord_itsm":       self.coord_itsm,
            "recurrence_count": self.recurrence_count,
            "last_seen_at":     self.last_seen_at.isoformat() if self.last_seen_at else None,
            "payload":          self.payload or {},
            "status":           self.status,
            "triggered_at":     self.triggered_at.isoformat(),
            "acknowledged_at":  self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by":  self.acknowledged_by,
            "resolved_at":      self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by":      self.resolved_by,
            "resolution":       self.resolution,
            "resolution_note":  self.resolution_note,
            "is_open":          self.status == "open",
        }


# ══════════════════════════════════════════════════════════════════════════════
#  PROVIDER REGISTRY TIER — replaces providers.yaml runtime configuration
# ══════════════════════════════════════════════════════════════════════════════

class Provider(Base):
    """
    DB-backed evaluation provider registry.

    Replaces the providers.yaml / config_service_configs('gs','providers') blob.
    The GS reads this table at startup and on hot-reload via GET /internal/reload.

    provider_type values:
      azure     — Azure Content Safety REST API
      aws       — AWS Bedrock Guardrail API
      sml       — Self-hosted small ML model (e.g. Llama Prompt Guard 86M)
      llm_judge — LLM-as-judge (cloud or local; model configured in extra_config)
      local     — In-process keyword / embedding classifier
      mock      — Mock provider (dev/test only)

    Credentials are NEVER stored here.  api_key_env_var holds the NAME of the
    environment variable; the GS reads os.getenv(api_key_env_var) at runtime.
    For cloud deployments the env var is injected by Secrets Manager / Key Vault
    at container start — the DB never sees the secret value.

    score_mapping format by provider_type:
      azure:
        [{"range": [lo, hi], "canonical": N, "status": "PASS|WARNING|FLAGGED"}, ...]
        lo/hi are the provider-native severity integers (Azure: 0-7)
      aws:
        {"type": "categorical",
         "map": {"HIGH": {"canonical": 8, "status": "FLAGGED"}, ...}}
        Keys are Bedrock confidence strings: CRITICAL | HIGH | MEDIUM | LOW
      sml:
        [{"range": [lo, hi], "canonical": N, "status": "..."}]
        lo/hi are model output probability thresholds (0.0-1.0)
      llm_judge:
        null — score is returned directly in the structured JSON response

    category_mapping format (azure and aws):
      {"Hate": "hate", "Sexual": "sexual_content", "MISCONDUCT": "misconduct", ...}
      Maps provider-native category/label names to canonical dimension names.

    extra_config format by provider_type:
      aws:       {"region": "us-east-2", "guardrail_arn": "...", "guardrail_version": "1"}
      sml:       {"model_path": "meta-llama/Prompt-Guard-86M", "device": "cpu"}
      llm_judge: {"model_name": "claude-3-5-haiku-20241022", "temperature": 0,
                  "max_tokens": 512, "prompt_template_id": "eu_ai_act_v1"}
    """
    __tablename__ = "config_providers"

    provider_id:   Mapped[str]            = mapped_column(String(64),  primary_key=True)
    provider_type: Mapped[str]            = mapped_column(String(32),  nullable=False, index=True)
    display_name:  Mapped[str]            = mapped_column(String(128), nullable=False)
    description:   Mapped[Optional[str]]  = mapped_column(Text,        nullable=True)

    active:        Mapped[bool]           = mapped_column(Boolean,     nullable=False, default=True)

    # Connection config — secrets via env var NAME only
    endpoint:          Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    api_key_env_var:   Mapped[Optional[str]] = mapped_column(String(64),  nullable=True)
    extra_config:      Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Capability manifest
    supported_dims:    Mapped[list]             = mapped_column(JSON, nullable=False, default=list)
    # Interception-point manifest — which ContentRole values this provider handles.
    # Empty list means "all roles" (universal).  Used alongside supported_dims to
    # validate assignments: provider must cover both the dimension AND its roles.
    supported_roles:   Mapped[Optional[list]]   = mapped_column(JSON, nullable=True)

    # Score and category translation mappings (moved from hardcoded Python)
    score_mapping:     Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    category_mapping:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Fail-open (default) or fail-closed on provider error
    fail_mode:         Mapped[str]            = mapped_column(String(16), nullable=False, default="fail_open")

    # Health tracking — updated by GS on each evaluation call
    health_status:         Mapped[str]            = mapped_column(String(16), nullable=False, default="healthy")
    consecutive_failures:  Mapped[int]            = mapped_column(Integer,    nullable=False, default=0)
    last_checked_at:       Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_failure_reason:   Mapped[Optional[str]]  = mapped_column(Text, nullable=True)

    created_at:    Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    updated_at:    Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        # credential_available: True if the expected env var is populated.
        # Providers that don't need a key (local / mock) have api_key_env_var=None
        # and are considered credentialed by default.
        if self.api_key_env_var:
            credential_available = bool(os.getenv(self.api_key_env_var))
        else:
            credential_available = True   # no key required (local / mock)

        # operational_state — single derived field consumed by the UI and GS:
        #   "available"   — credentialed AND active; GS will route to this provider
        #   "disabled"    — credentialed but operator turned it off; re-enable allowed
        #   "unavailable" — credentials missing; toggle is blocked until env var is set
        if not credential_available:
            operational_state = "unavailable"   # credentials gate everything
        elif self.active:
            operational_state = "available"
        else:
            operational_state = "disabled"

        return {
            "provider_id":    self.provider_id,
            "provider_type":  self.provider_type,
            "display_name":   self.display_name,
            "description":    self.description,
            "active":         self.active,
            "endpoint":       self.endpoint,
            # expose env var NAME (not value) so UI can tell operators which var to set
            "api_key_env_var":      self.api_key_env_var,
            "credential_available": credential_available,
            "operational_state":    operational_state,
            "supported_dims":  self.supported_dims or [],
            "supported_roles": self.supported_roles or [],
            "score_mapping":  self.score_mapping,
            "category_mapping": self.category_mapping,
            "fail_mode":      self.fail_mode,
            "health": {
                "status":               self.health_status,
                "consecutive_failures": self.consecutive_failures,
                "last_checked_at":      self.last_checked_at.isoformat() if self.last_checked_at else None,
                "last_failure_reason":  self.last_failure_reason,
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  AGENT REGISTRY TIER — discovered and declared agent topology
# ══════════════════════════════════════════════════════════════════════════════

class AppAgent(Base):
    """
    Registered agent topology for an application.

    Written by the RAI Trust SDK on first invocation of GovernedGraph via
    POST /internal/register-agents.  One row per (app_id, agent_name).

    is_declared_agent  True  — explicitly listed in agent_nodes or a2a_tools.values();
                               this node has governance semantics (tool calls, A2A handoffs).
                       False — discovered from graph.nodes.keys() but not declared as an
                               agent (pure routing node — only LLM attribution present).

    declared_tools     Tools in a2a_tools that route TO this agent (incoming entry-point
                       tools, e.g. "international_transfer" → intl_wire_agent).

    a2a_targets        Agent names reachable from this app's A2A topology
                       (populated for declared agents from a2a_tools.values()).

    all_nodes          Full list of discovered graph nodes at registration time;
                       same value for all rows of the same app_id (informational).
    """
    __tablename__ = "config_app_agents"
    __table_args__ = (
        UniqueConstraint("app_id", "agent_name", name="uq_app_agent"),
    )

    id:                Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id:            Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    agent_name:        Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    # True = in agent_nodes or a2a_tools.values(); False = routing / utility node
    is_declared_agent: Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    # JSON list of tool names whose a2a_tools value == this agent_name
    declared_tools:    Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # JSON list of agent names reachable via A2A from this app
    a2a_targets:       Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Full discovered node list for the graph (same for all rows of same app_id)
    all_nodes:         Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    first_seen_at:     Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now)
    last_seen_at:      Mapped[datetime]       = mapped_column(DateTime, nullable=False, default=_now,
                                                               onupdate=_now)
    # ── Lifecycle / reconciliation ────────────────────────────────────────────
    # status:           active   — present in the app's latest declaration
    #                   observed — real node seen in traffic but not declared
    #                   retired  — was declared, now absent (removed from the app)
    #                   shadow   — acts in A2A traffic but isn't declared (governance flag)
    # last_declared_at: last time this agent appeared in a register-agents declaration
    #                   (drives GRACE-based retirement — see register_agents()).
    # retired_at:       when reconciliation marked it retired (None while live).
    status:            Mapped[str]                = mapped_column(String(16), nullable=False,
                                                                  default="active", index=True)
    last_declared_at:  Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    retired_at:        Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "app_id":            self.app_id,
            "agent_name":        self.agent_name,
            "is_declared_agent": self.is_declared_agent,
            "declared_tools":    self.declared_tools or [],
            "a2a_targets":       self.a2a_targets or [],
            "all_nodes":         self.all_nodes or [],
            "status":            self.status,
            "first_seen_at":     self.first_seen_at.isoformat(),
            "last_seen_at":      self.last_seen_at.isoformat(),
            "last_declared_at":  self.last_declared_at.isoformat() if self.last_declared_at else None,
            "retired_at":        self.retired_at.isoformat() if self.retired_at else None,
        }


class AgentCard(Base):
    """
    Per-agent governance metadata for the Agent Card / Agent Registry.

    Holds the SEEDED / operator-authored attributes. The runtime fields
    (status, eval/violation/authority-breach counts, trust score) are computed
    at read time from config_app_agents + audit_evaluation_audit, not stored.
    One row per (app_id, agent_id); agent_id MUST match config_app_agents.agent_name
    (and the tools shown come from config_app_agents.declared_tools).
    """
    __tablename__ = "config_agent_cards"
    __table_args__ = (
        UniqueConstraint("app_id", "agent_id", name="uq_agent_card"),
    )

    id:                 Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id:             Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    agent_id:           Mapped[str]            = mapped_column(String(128), nullable=False, index=True)
    name:               Mapped[str]            = mapped_column(String(128), nullable=False)
    agent_type:         Mapped[str]            = mapped_column(String(32),  nullable=False, default="llm")
    capability:         Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)
    purpose:            Mapped[Optional[str]]  = mapped_column(Text,        nullable=True)
    framework:          Mapped[str]            = mapped_column(String(48),  nullable=False, default="LangGraph")
    model_id:           Mapped[Optional[str]]  = mapped_column(String(64),  nullable=True)
    domain:             Mapped[Optional[str]]  = mapped_column(String(48),  nullable=True)
    data_access:        Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    escalation:         Mapped[Optional[str]]  = mapped_column(String(160), nullable=True)
    allowed_actions:    Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    prohibited_actions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    max_authority:      Mapped[Optional[str]]  = mapped_column(String(32),  nullable=True)
    # Governance config flags (operator-toggleable from the agent card). Declarative:
    # kill_switch = "this agent CAN be terminated by the platform".
    # hitl_mode = how a human enters the loop — one of:
    #   none      → runs autonomously
    #   oversight → the platform can pause it for human review at any time (external)
    #   inherent  → human checkpoints are built into the agent's own workflow ("By design")
    #   both      → inherent checkpoints + platform oversight
    # hitl_escalation is kept (legacy boolean) but now DERIVED from hitl_mode != 'none'.
    kill_switch:        Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    hitl_escalation:    Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    hitl_mode:          Mapped[str]            = mapped_column(String(16), nullable=False, default="none")
    created_at:         Mapped[datetime]       = mapped_column(DateTime, default=_now)
    updated_at:         Mapped[datetime]       = mapped_column(DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "app_id":             self.app_id,
            "agent_id":           self.agent_id,
            "name":               self.name,
            "type":               self.agent_type,
            "capability":         self.capability,
            "purpose":            self.purpose,
            "framework":          self.framework,
            "model_id":           self.model_id,
            "domain":             self.domain,
            "data_access":        self.data_access or [],
            "escalation":         self.escalation,
            "allowed_actions":    self.allowed_actions or [],
            "prohibited_actions": self.prohibited_actions or [],
            "max_authority":      self.max_authority,
            "kill_switch":        bool(self.kill_switch),
            "hitl_mode":          self.hitl_mode or "none",
            "hitl_escalation":    (self.hitl_mode or "none") != "none",
        }
