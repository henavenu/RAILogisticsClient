"""
PAP inbound request schemas (Pydantic v2).
"""
from typing import Optional, List
from pydantic import BaseModel, Field, model_validator


class ThresholdBands(BaseModel):
    permissive: List[float] = Field(default=[1.0, 4.0], min_length=2, max_length=2)
    normative:  List[float] = Field(default=[5.0, 6.0], min_length=2, max_length=2)
    coercive:   List[float] = Field(default=[7.0, 9.0], min_length=2, max_length=2)

    @model_validator(mode="after")
    def check_ranges(self) -> "ThresholdBands":
        for band, values in [("permissive", self.permissive), ("normative", self.normative), ("coercive", self.coercive)]:
            lo, hi = values
            if lo > hi:
                raise ValueError(f"{band}: lo ({lo}) must be <= hi ({hi})")
            # Scores are 1-9; a bound of 10 is the "never matches / band disabled"
            # sentinel — advisory-only dimensions set coercive=[10,10] so they never
            # block. Accept 10 so those dimensions can be saved.
            if not (1 <= lo <= 10 and 1 <= hi <= 10):
                raise ValueError(f"{band}: values must be in range [1, 10] (10 = disabled)")
        return self


class PolicyCreate(BaseModel):
    dimension:             str   = Field(..., min_length=1, max_length=64)
    app_id:                Optional[str] = Field(None, max_length=128)
    severity:              int   = Field(default=1, ge=1, le=3)
    thresholds:            ThresholdBands = Field(default_factory=ThresholdBands)
    remediation_normative: Optional[str] = Field(None, max_length=1000)
    remediation_coercive:  Optional[str] = Field(None, max_length=1000)
    # MessageWriter fields (migration 0017)
    policy_code:           Optional[str] = Field(None, max_length=20)
    normative_action:      str           = Field(default="REVISION", pattern="^(REVISION|HITL)$")
    normative_guidance:    Optional[str] = Field(None, max_length=1000)
    human_note:            Optional[str] = Field(None, max_length=2000)
    ops_alert:             bool          = False
    hitl_trigger:          bool          = False
    incident_routing:      str           = Field(default="platform_dashboard", pattern="^(none|servicenow|platform_dashboard)$")


class PolicyUpdate(BaseModel):
    severity:              Optional[int]          = Field(None, ge=1, le=3)
    thresholds:            Optional[ThresholdBands] = None
    remediation_normative: Optional[str]          = Field(None, max_length=1000)
    remediation_coercive:  Optional[str]          = Field(None, max_length=1000)
    # MessageWriter fields (migration 0017)
    policy_code:           Optional[str]          = Field(None, max_length=20)
    normative_action:      Optional[str]          = Field(None, pattern="^(REVISION|HITL)$")
    normative_guidance:    Optional[str]          = Field(None, max_length=1000)
    active:                Optional[bool]         = None
    hitl_trigger:          Optional[bool]         = None
    human_note:            Optional[str]          = Field(None, max_length=2000)
    ops_alert:             Optional[bool]         = None
    incident_routing:      Optional[str]          = Field(None, pattern="^(none|servicenow|platform_dashboard)$")


class EvaluatorPolicyUpsert(BaseModel):
    """
    Body for POST /v1/evaluators/{dimension}/policy.

    Identical to PolicyCreate but without the dimension field (it comes from
    the path) and with an optional publish flag so the caller can create the
    policy and hot-reload the Policy Engine in a single request.
    """
    app_id:                Optional[str]   = Field(None, max_length=128)
    severity:              int             = Field(default=2, ge=1, le=3)
    thresholds:            ThresholdBands  = Field(default_factory=ThresholdBands)
    remediation_normative: Optional[str]   = Field(None, max_length=1000)
    remediation_coercive:  Optional[str]   = Field(None, max_length=1000)
    # MessageWriter fields (migration 0017)
    policy_code:           Optional[str]   = Field(None, max_length=20)
    normative_action:      str             = Field(default="REVISION", pattern="^(REVISION|HITL)$")
    normative_guidance:    Optional[str]   = Field(None, max_length=1000)
    human_note:            Optional[str]   = Field(None, max_length=2000)
    ops_alert:             bool            = False
    incident_routing:      str             = Field(
        default="platform_dashboard", pattern="^(none|servicenow|platform_dashboard)$"
    )
    publish:               bool            = Field(
        default=True,
        description="When True (default), publish policies.yaml and hot-reload the Policy Engine immediately.",
    )


class PublishRequest(BaseModel):
    notes: Optional[str] = Field(None, max_length=500)


class AppUpsert(BaseModel):
    app_id:             str  = Field(..., min_length=1, max_length=128)
    display_name:       Optional[str] = Field(None, max_length=256)
    base_tier:          str  = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH)$")
    dynamic_evaluation: bool = False


# Backward-compat alias
AppConfigUpsert = AppUpsert


# ── Evaluation dimension config ────────────────────────────────────────────────

CONTENT_ROLES = {"user_prompt", "llm_response", "tool_call", "agent_to_agent"}


class EvaluatorRolesUpdate(BaseModel):
    """
    Body for PATCH /v1/evaluators/{dimension_id}.

    Allows operators to reassign which interception points (content roles)
    a dimension is evaluated on, toggle its active/deferred status, and
    reassign the provider responsible for evaluating it.
    All fields are optional — send only what you want to change.
    """
    supported_roles: Optional[List[str]] = Field(
        None,
        description="Subset of [user_prompt, llm_response, tool_call, agent_to_agent].",
    )
    status: Optional[str] = Field(
        None,
        description="'active' to enforce the dimension; 'deferred' to score-only (no verdicts).",
    )
    provider: Optional[str] = Field(
        None,
        max_length=64,
        description="Provider ID to assign for this dimension (must exist in config_providers).",
    )

    @model_validator(mode="after")
    def _validate(self) -> "EvaluatorRolesUpdate":
        if self.supported_roles is not None:
            invalid = [r for r in self.supported_roles if r not in CONTENT_ROLES]
            if invalid:
                raise ValueError(
                    f"Invalid role(s): {invalid}. "
                    f"Must be a subset of {sorted(CONTENT_ROLES)}."
                )
        if self.status is not None and self.status not in ("active", "deferred"):
            raise ValueError("status must be 'active' or 'deferred'")
        return self
