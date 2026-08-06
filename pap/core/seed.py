"""
DB seeder — populates all config tables from YAML source files on first run.

Seeding order respects FK dependencies:
  1. config_evaluation_dimensions  ← evaluations.yaml  (GS)
  2. config_threshold_profiles     ← threshold_profiles.yaml (PE)
  3. config_policy_packs           ← policy_packs.yaml  (PE)
  4. config_pack_dimensions        ← policy_packs.yaml  (PE) — after 1,2,3
  5. config_apps                   ← app_stacks.yaml    (PE) — inferred from app keys
  6. config_app_pack_assignments   ← app_stacks.yaml    (PE) — after 3,5
  7. config_policies               ← policies.yaml      (PE) — after 1
  8. config_session_policy         ← session_policy.yaml(PE) — singleton

Each seeder is idempotent — existing rows are never overwritten so operator
changes made via the PAP UI survive service restarts.
"""
from __future__ import annotations

import logging
import yaml
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session

from core.models import (
    EvaluationDimension,
    ThresholdProfile,
    PolicyPack,
    PackDimension,
    App,
    AppPackAssignment,
    Policy,
    SessionPolicyConfig,
    ServiceConfig,
    Provider,
)

# Column printed in stub-policy warnings so operators know where to look
_STUB_NOTE = (
    "Auto-generated stub — add remediation text and publish via the PAP UI."
)

logger = logging.getLogger(__name__)

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent  # rai-trust-platform/
_PE_CONFIG     = _PLATFORM_ROOT / "policy-engine"  / "config"
_GS_CONFIG     = _PLATFORM_ROOT / "guardrail-service" / "config"


# ── helpers ────────────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    if not path.exists():
        logger.warning(f"Seed file not found: {path}")
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _now() -> datetime:
    return datetime.utcnow()


# ── 1. Evaluation dimensions ───────────────────────────────────────────────────

def seed_dimensions(db: Session) -> int:
    """Seed config_evaluation_dimensions from guardrail-service/config/evaluations.yaml."""
    raw    = _load(_GS_CONFIG / "evaluations.yaml")
    cats   = raw.get("categories", {})
    existing = {r.dimension_id for r in db.query(EvaluationDimension.dimension_id).all()}
    count  = 0

    for category, dims in cats.items():
        for dim_id, cfg in (dims or {}).items():
            if dim_id in existing:
                continue
            db.add(EvaluationDimension(
                dimension_id     = dim_id,
                category         = category,
                display_name     = dim_id.replace("_", " ").title(),
                description      = cfg.get("description"),
                evaluator_type   = cfg.get("evaluator_type", "gs_call"),
                provider         = cfg.get("provider", "mock_provider"),
                status           = cfg.get("status", "active"),
                supported_roles  = cfg.get("roles", []),
                requires_context = bool(cfg.get("requires_context", False)),
                created_at       = _now(),
                updated_at       = _now(),
            ))
            count += 1

    if count:
        db.commit()
        logger.info(f"Seeded {count} evaluation dimensions")
    return count


# ── 2. Threshold profiles ──────────────────────────────────────────────────────

def seed_threshold_profiles(db: Session) -> int:
    """Seed config_threshold_profiles from policy-engine/config/threshold_profiles.yaml."""
    raw      = _load(_PE_CONFIG / "threshold_profiles.yaml")
    profiles = raw.get("profiles", {})
    existing = {r.profile_id for r in db.query(ThresholdProfile.profile_id).all()}
    count    = 0

    for pid, cfg in profiles.items():
        if pid in existing:
            continue
        # Use step "0" (base) thresholds
        base = cfg.get("step_map", {}).get("0", cfg)
        perm = base.get("permissive", cfg.get("permissive", [1, 4]))
        norm = base.get("normative",  cfg.get("normative",  [5, 6]))
        coer = base.get("coercive",   cfg.get("coercive",   [7, 9]))

        db.add(ThresholdProfile(
            profile_id    = pid,
            permissive_lo = float(perm[0]),
            permissive_hi = float(perm[1]),
            normative_lo  = float(norm[0]),
            normative_hi  = float(norm[1]),
            coercive_lo   = float(coer[0]),
            coercive_hi   = float(coer[1]),
            description   = None,
            created_at    = _now(),
            updated_at    = _now(),
        ))
        count += 1

    if count:
        db.commit()
        logger.info(f"Seeded {count} threshold profiles")
    return count


# ── 3 + 4. Policy packs and pack dimensions ────────────────────────────────────

def seed_policy_packs(db: Session) -> int:
    """Seed config_policy_packs and config_pack_dimensions from policy_packs.yaml."""
    raw   = _load(_PE_CONFIG / "policy_packs.yaml")
    packs = raw.get("packs", {})

    # Fetch existing keys to avoid duplicates
    existing_packs = {r.pack_id for r in db.query(PolicyPack.pack_id).all()}
    existing_dims  = {
        (r.pack_id, r.dimension_id)
        for r in db.query(PackDimension.pack_id, PackDimension.dimension_id).all()
    }
    # Also fetch known dimension_ids and profile_ids for FK safety
    known_dims     = {r.dimension_id for r in db.query(EvaluationDimension.dimension_id).all()}
    known_profiles = {r.profile_id   for r in db.query(ThresholdProfile.profile_id).all()}

    pack_count = dim_count = 0

    # ── Pass 1: insert PolicyPack rows ────────────────────────────────────────
    # Must be committed (or flushed) before PackDimension rows can reference them
    # via the pack_id FK.  SQLAlchemy without relationship() declarations cannot
    # infer the correct insert order, so we split into two passes.
    for pack_id, pcfg in packs.items():
        if pack_id not in existing_packs:
            db.add(PolicyPack(
                pack_id             = pack_id,
                display_name        = pack_id.replace("_", " ").title(),
                description         = pcfg.get("description"),
                layer               = pcfg.get("layer", "domain-addon"),
                enforcement         = pcfg.get("enforcement", "flexible"),
                mode                = pcfg.get("mode", "collect-all"),
                combining_algorithm = pcfg.get("combining_algorithm", "worst_case"),
                hitl_trigger        = bool(pcfg.get("hitl_trigger", False)),
                owner               = pcfg.get("owner"),
                version             = str(pcfg.get("version", "1.0.0")),
                effective_date      = pcfg.get("effective"),
                active              = bool(pcfg.get("active", True)),
                created_at          = _now(),
                updated_at          = _now(),
            ))
            pack_count += 1

    # Flush packs to DB within the transaction so FK refs are satisfied
    if pack_count:
        db.flush()
        # Refresh known_packs so we can verify pack_id FKs for dimensions
        known_packs_now = {r.pack_id for r in db.query(PolicyPack.pack_id).all()}
    else:
        known_packs_now = existing_packs

    # ── Pass 2: insert PackDimension rows ────────────────────────────────────
    for pack_id, pcfg in packs.items():
        for dim_cfg in pcfg.get("dimensions", []):
            dim_id     = dim_cfg.get("id")
            profile_id = dim_cfg.get("profile", "standard")

            if not dim_id:
                continue
            if (pack_id, dim_id) in existing_dims:
                continue

            # FK safety: skip if pack, dimension, or profile not in catalog
            if pack_id not in known_packs_now:
                logger.warning(
                    f"[seed] PackDimension skipped: pack '{pack_id}' not in DB after flush"
                )
                continue
            if dim_id not in known_dims:
                logger.warning(
                    f"[seed] Pack '{pack_id}' references unknown dimension '{dim_id}' "
                    f"— skipping (add dimension to evaluations.yaml first)"
                )
                continue
            if profile_id not in known_profiles:
                logger.warning(
                    f"[seed] Pack '{pack_id}' dimension '{dim_id}' references unknown "
                    f"profile '{profile_id}' — falling back to 'standard'"
                )
                profile_id = "standard" if "standard" in known_profiles else next(iter(known_profiles), None)
                if not profile_id:
                    continue

            db.add(PackDimension(
                pack_id      = pack_id,
                dimension_id = dim_id,
                profile_id   = profile_id,
                severity     = int(dim_cfg.get("severity", 2)),
                abort        = bool(dim_cfg.get("abort", False)),
                active_roles = dim_cfg.get("roles", []),
                note         = dim_cfg.get("note"),
                created_at   = _now(),
                updated_at   = _now(),
            ))
            dim_count += 1

    if pack_count or dim_count:
        db.commit()
        logger.info(f"Seeded {pack_count} policy packs, {dim_count} pack dimensions")
    return pack_count + dim_count


# ── 5 + 6. Apps and pack assignments ──────────────────────────────────────────

def seed_apps(db: Session) -> int:
    """Seed config_apps and config_app_pack_assignments from app_stacks.yaml."""
    raw        = _load(_PE_CONFIG / "app_stacks.yaml")
    app_stacks = raw.get("app_stacks", {})

    existing_apps  = {r.app_id   for r in db.query(App.app_id).all()}
    existing_assns = {
        (r.app_id, r.pack_id)
        for r in db.query(AppPackAssignment.app_id, AppPackAssignment.pack_id).all()
    }
    known_packs = {r.pack_id for r in db.query(PolicyPack.pack_id).all()}

    app_count  = assn_count = 0

    for app_id, acfg in app_stacks.items():
        if app_id not in existing_apps:
            db.add(App(
                app_id             = app_id,
                display_name       = app_id.replace("_", " ").title(),
                base_tier          = "MEDIUM",
                enforcement_mode   = "BLOCK",
                dynamic_evaluation = False,
                active             = True,
                created_at         = _now(),
                updated_at         = _now(),
            ))
            app_count += 1

        for pack_id, pack_cfg in (acfg.get("assignments") or {}).items():
            if (app_id, pack_id) in existing_assns:
                continue
            if pack_id not in known_packs:
                logger.warning(
                    f"[seed] App '{app_id}' references unknown pack '{pack_id}' — skipping"
                )
                continue
            db.add(AppPackAssignment(
                app_id      = app_id,
                pack_id     = pack_id,
                active      = bool(pack_cfg.get("active", True)),
                assigned_at = _now(),
                assigned_by = "seed",
            ))
            assn_count += 1

    if app_count or assn_count:
        db.commit()
        logger.info(f"Seeded {app_count} apps, {assn_count} pack assignments")
    return app_count + assn_count


# ── 7. Policies ────────────────────────────────────────────────────────────────

def seed_policies(db: Session) -> int:
    """Seed config_policies from policy-engine/config/policies.yaml."""
    raw      = _load(_PE_CONFIG / "policies.yaml")
    policies = raw.get("policies", {})

    existing     = {r.dimension for r in db.query(Policy.dimension).filter(Policy.app_id == None).all()}
    known_dims   = {r.dimension_id for r in db.query(EvaluationDimension.dimension_id).all()}
    count        = 0

    for dim, cfg in policies.items():
        if dim in existing:
            continue
        if dim not in known_dims:
            logger.warning(
                f"[seed] policies.yaml references unknown dimension '{dim}' "
                f"— skipping (add to evaluations.yaml first)"
            )
            continue

        t = cfg.get("thresholds", {})
        r = cfg.get("remediations", {})

        db.add(Policy(
            dimension             = dim,
            app_id                = None,
            severity              = int(cfg.get("severity", 1)),
            permissive_lo         = float(t.get("permissive", [1, 4])[0]),
            permissive_hi         = float(t.get("permissive", [1, 4])[1]),
            normative_lo          = float(t.get("normative",  [5, 6])[0]),
            normative_hi          = float(t.get("normative",  [5, 6])[1]),
            coercive_lo           = float(t.get("coercive",   [7, 9])[0]),
            coercive_hi           = float(t.get("coercive",   [7, 9])[1]),
            remediation_normative = r.get("normative"),
            remediation_coercive  = r.get("coercive"),
            active                = True,
            version               = 1,
            published_at          = _now(),
            created_at            = _now(),
            updated_at            = _now(),
        ))
        count += 1

    if count:
        db.commit()
        logger.info(f"Seeded {count} policies from policies.yaml")
    return count


# ── 8. Session policy (singleton) ─────────────────────────────────────────────

def seed_session_policy(db: Session) -> int:
    """Seed config_session_policy singleton from session_policy.yaml."""
    existing = db.query(SessionPolicyConfig).filter(SessionPolicyConfig.id == 1).first()
    if existing:
        return 0

    raw   = _load(_PE_CONFIG / "session_policy.yaml")
    dedup = raw.get("dedup", {})
    deltas = raw.get("trust_deltas", {})

    db.add(SessionPolicyConfig(
        id                         = 1,
        trust_delta_coercive       = float(deltas.get("COERCIVE",   -0.20)),
        trust_delta_normative      = float(deltas.get("NORMATIVE",  -0.05)),
        trust_delta_permissive     = float(deltas.get("PERMISSIVE",  0.02)),
        trust_floor                = float(raw.get("trust_floor",   0.20)),
        trust_ceiling              = float(raw.get("trust_ceiling", 1.0)),
        session_ttl_seconds        = int(raw.get("session_ttl_seconds", 1800)),
        dedup_exact_block          = bool(dedup.get("exact_block", True)),
        dedup_similarity_threshold = float(dedup.get("similarity_threshold", 0.92)),
        dedup_window               = int(dedup.get("window", 50)),
        dedup_repetition_penalty   = float(dedup.get("repetition_penalty", -0.05)),
        updated_at                 = _now(),
        updated_by                 = "seed",
    ))
    db.commit()
    logger.info("Seeded session_policy singleton")
    return 1


# ── 9. Stub policies for any pack dimension without a policy ──────────────────

def seed_stub_policies(db: Session) -> int:
    """
    Auto-create stub Policy rows for every PackDimension that has no corresponding
    global policy.  Runs after seed_policies() so explicit YAML definitions always
    take precedence — this function only fills the remaining gaps.

    Thresholds are derived from the pack dimension's ThresholdProfile so the
    verdict logic is immediately consistent.  Remediations are left empty (None)
    and published_at is NULL — the policy is in DRAFT state until an operator
    adds remediations and publishes via the PAP UI.

    This mirrors the pack→app and dim→pack patterns: the relationship is governed
    and auditable from creation, not retrofitted after the fact.
    """
    # Existing global policies (app_id=None)
    existing = {
        r.dimension
        for r in db.query(Policy.dimension).filter(Policy.app_id.is_(None)).all()
    }

    # All pack dimensions with their profiles, deduplicated by dimension_id
    # (same dimension can appear in multiple packs — use the highest severity seen)
    pack_dim_map: dict[str, tuple[str, str, int]] = {}  # dim_id → (pack_id, profile_id, severity)
    for pd in db.query(PackDimension).all():
        existing_entry = pack_dim_map.get(pd.dimension_id)
        if existing_entry is None or pd.severity > existing_entry[2]:
            pack_dim_map[pd.dimension_id] = (pd.pack_id, pd.profile_id, pd.severity)

    # All threshold profiles keyed by profile_id
    profiles: dict[str, ThresholdProfile] = {
        p.profile_id: p for p in db.query(ThresholdProfile).all()
    }

    count = 0
    for dim_id, (pack_id, profile_id, severity) in sorted(pack_dim_map.items()):
        if dim_id in existing:
            continue

        profile = profiles.get(profile_id)
        if not profile:
            logger.warning(
                f"[seed] stub_policy: unknown profile '{profile_id}' for "
                f"'{dim_id}' (pack={pack_id}) — skipping"
            )
            continue

        db.add(Policy(
            dimension             = dim_id,
            app_id                = None,
            severity              = severity,
            permissive_lo         = profile.permissive_lo,
            permissive_hi         = profile.permissive_hi,
            normative_lo          = profile.normative_lo,
            normative_hi          = profile.normative_hi,
            coercive_lo           = profile.coercive_lo,
            coercive_hi           = profile.coercive_hi,
            remediation_normative = None,   # draft — operator fills these in
            remediation_coercive  = None,
            human_note            = _STUB_NOTE,
            active                = True,
            version               = 1,
            published_at          = None,   # draft until explicitly published
            created_at            = _now(),
            updated_at            = _now(),
        ))
        count += 1
        logger.info(
            f"[seed] Stub policy created for '{dim_id}' "
            f"(pack={pack_id}, profile={profile_id}, severity={severity})"
        )

    if count:
        db.commit()
        logger.info(f"Seeded {count} stub policies from pack dimensions")
    return count


# ── 10. Service config blobs (replaces YAML runtime stores) ───────────────────

def seed_service_configs(db: Session) -> int:
    """
    Seed config_service_configs from YAML files for PE and GS components.

    Each YAML is parsed once and stored as a JSON blob.  After the first run,
    all services read from DB — the YAML files are never read again at runtime.

    Blobs seeded:
      pe / threshold_profiles  — full threshold_profiles.yaml (incl. step_map)
      pe / policy_packs        — full policy_packs.yaml (incl. context_adjustments)
      pe / context_routing     — context_routing.yaml
      pe / tool_risk           — tool_risk_registry.yaml (runtime source of truth)
      gs / mock_scenarios      — mock_scenarios.yaml
      gs / embedding_filter    — embedding_filter.yaml
      gs / providers           — providers.yaml (env-var refs only, no secrets)
    """
    existing = {
        (r.service_name, r.config_key)
        for r in db.query(ServiceConfig.service_name, ServiceConfig.config_key).all()
    }
    count = 0

    blobs = [
        ("pe", "threshold_profiles", _PE_CONFIG / "threshold_profiles.yaml"),
        ("pe", "policy_packs",       _PE_CONFIG / "policy_packs.yaml"),
        ("pe", "context_routing",    _PE_CONFIG / "context_routing.yaml"),
        ("pe", "tool_risk",          _PE_CONFIG / "tool_risk_registry.yaml"),
        ("gs", "mock_scenarios",     _GS_CONFIG / "mock_scenarios.yaml"),
        ("gs", "embedding_filter",   _GS_CONFIG / "embedding_filter.yaml"),
        ("gs", "providers",          _GS_CONFIG / "providers.yaml"),
    ]

    for service_name, config_key, path in blobs:
        if (service_name, config_key) in existing:
            continue
        data = _load(path)
        if not data:
            logger.warning(f"[seed] ServiceConfig: {path} not found or empty — skipping")
            continue
        db.add(ServiceConfig(
            service_name = service_name,
            config_key   = config_key,
            config_json  = data,
            seeded_from  = str(path),
            created_at   = _now(),
            updated_at   = _now(),
        ))
        count += 1
        logger.debug(f"[seed] ServiceConfig: seeded ({service_name}, {config_key})")

    if count:
        db.commit()
        logger.info(f"Seeded {count} service config blob(s)")
    return count


# ── 11. Provider registry ─────────────────────────────────────────────────────

def seed_providers(db: Session) -> int:
    """
    Seed config_providers with all known evaluation providers.

    Score/category mappings are moved here from hardcoded Python dicts
    in the provider adapter files.  The GS reads these at startup so
    the adapters no longer hard-code translation tables.

    Credentials are NEVER stored — only the env-var NAME is recorded.
    """
    existing = {r.provider_id for r in db.query(Provider.provider_id).all()}
    count = 0

    providers = [
        # ── Azure Content Safety ──────────────────────────────────────────────
        Provider(
            provider_id   = "azure_content_safety",
            provider_type = "azure",
            display_name  = "Azure Content Safety",
            description   = (
                "Microsoft Azure Content Safety API. Evaluates hate, harassment, "
                "sexual content, self-harm, and violence. Returns numeric severity 0-7."
            ),
            active          = True,
            endpoint        = None,           # runtime: os.getenv("AZURE_ENDPOINT")
            api_key_env_var = "AZURE_API_KEY",
            extra_config    = {"endpoint_env_var": "AZURE_ENDPOINT"},
            supported_dims  = ["hate", "harassment", "sexual_content", "self_harm", "violence"],
            supported_roles = ["user_prompt", "llm_response"],
            # Azure returns numeric severity 0-7 mapped to canonical 1-9
            score_mapping = [
                {"range": [0, 1], "canonical": 1, "status": "PASS"},
                {"range": [2, 3], "canonical": 3, "status": "PASS"},
                {"range": [4, 5], "canonical": 5, "status": "WARNING"},
                {"range": [6, 7], "canonical": 8, "status": "FLAGGED"},
            ],
            # Azure category name -> canonical dimension name
            category_mapping = {
                "Hate":       "hate",
                "Sexual":     "sexual_content",
                "Violence":   "violence",
                "SelfHarm":   "self_harm",
                "Harassment": "harassment",
            },
            fail_mode            = "fail_open",
            health_status        = "healthy",
            consecutive_failures = 0,
            created_at = _now(), updated_at = _now(),
        ),

        # ── AWS Bedrock Guardrail ─────────────────────────────────────────────
        Provider(
            provider_id   = "aws_bedrock_guardrail",
            provider_type = "aws",
            display_name  = "AWS Bedrock Guardrail",
            description   = (
                "Amazon Bedrock Guardrails API. Evaluates misconduct, PII/data-leakage, "
                "prompt injection, jailbreak, and malicious intent. "
                "Returns categorical confidence: CRITICAL | HIGH | MEDIUM | LOW."
            ),
            active          = True,
            endpoint        = None,
            api_key_env_var = "AWS_ACCESS_KEY_ID",
            extra_config    = {
                "aws_secret_env_var":   "AWS_SECRET_KEY",
                "guardrail_arn_env_var": "AWS_BEDROCK_GUARDRAIL_ARN",
                "region":               "us-east-2",
                "guardrail_version":    "1",
            },
            supported_dims  = [
                "misconduct", "sensitive_data_disclosure", "sensitive_data_request",
                "prompt_injection", "jailbreak", "malicious_intent",
            ],
            supported_roles = ["user_prompt", "llm_response"],
            # AWS returns categorical confidence — mapped to canonical 1-9
            score_mapping = {
                "type": "categorical",
                "map": {
                    "CRITICAL": {"canonical": 9, "status": "FLAGGED"},
                    "HIGH":     {"canonical": 8, "status": "FLAGGED"},
                    "MEDIUM":   {"canonical": 5, "status": "WARNING"},
                    "LOW":      {"canonical": 2, "status": "PASS"},
                },
            },
            # Bedrock category name -> canonical dimension name
            category_mapping = {
                "MISCONDUCT":       "misconduct",
                "HATE":             "hate",
                "SEXUAL":           "sexual_content",
                "VIOLENCE":         "violence",
                "SELF_HARM":        "self_harm",
                "HARASSMENT":       "harassment",
                "PROMPT_ATTACK":    "prompt_injection",
                "PROMPT_INJECTION": "prompt_injection",
                "JAILBREAK":        "jailbreak",
            },
            fail_mode            = "fail_open",
            health_status        = "healthy",
            consecutive_failures = 0,
            created_at = _now(), updated_at = _now(),
        ),

        # ── Trust Platform Provider (provider_id kept as 'mock_provider') ──────
        Provider(
            provider_id   = "mock_provider",
            provider_type = "mock",
            display_name  = "Trust Platform Provider",
            description   = (
                "The Trust Platform's built-in content-aware evaluator. Uses keyword "
                "signals per dimension; covers all dimensions not served by a specialist provider."
            ),
            active          = True,
            supported_dims  = [],   # empty = no filter; accepts any dimension (universal fallback)
            supported_roles = [],   # empty = universal; handles all interception points
            score_mapping    = None,
            category_mapping = None,
            fail_mode        = "fail_open",
            health_status    = "healthy",
            created_at = _now(), updated_at = _now(),
        ),

        # ── Local Policy Classifier ───────────────────────────────────────────
        Provider(
            provider_id   = "local_policy_classifier",
            provider_type = "local",
            display_name  = "Local Policy Classifier",
            description   = (
                "In-process keyword-based classifier for corporate policy enforcement. "
                "Zero external dependencies; no API latency."
            ),
            active          = True,
            supported_dims  = ["corporate_policy"],
            supported_roles = ["user_prompt", "llm_response", "tool_call", "agent_to_agent"],
            score_mapping    = None,
            category_mapping = None,
            fail_mode        = "fail_open",
            health_status    = "healthy",
            created_at = _now(), updated_at = _now(),
        ),

        # ── Embedding Filter ──────────────────────────────────────────────────
        Provider(
            provider_id   = "embedding_filter",
            provider_type = "local",
            display_name  = "Embedding Filter",
            description   = (
                "Semantic similarity filter using all-MiniLM-L6-v2 embeddings. "
                "Detects off-topic requests by comparing against allowed topic seeds. "
                "Fails open (score=1, status=UNKNOWN) if sentence-transformers unavailable."
            ),
            active          = True,
            supported_dims  = ["off_topic_request", "prompt_repetition"],
            supported_roles = ["user_prompt"],
            score_mapping    = None,
            category_mapping = None,
            fail_mode        = "fail_open",
            health_status    = "healthy",
            created_at = _now(), updated_at = _now(),
        ),

        # ── Prompt Guard SML ─────────────────────────────────────────────────
        # Small ML model (86M params) purpose-built for prompt injection and
        # jailbreak detection.  Replaces AWS Bedrock for these two critical dims.
        # Runs in-process on CPU — no external API, no per-call cost.
        # active=False until model is downloaded and device is configured.
        Provider(
            provider_id   = "prompt_guard",
            provider_type = "sml",
            display_name  = "Llama Prompt Guard",
            description   = (
                "Meta Llama Prompt Guard 86M — fine-tuned safety classifier for "
                "prompt injection and jailbreak detection. "
                "Runs in-process; model loaded from HuggingFace on first use. "
                "Replaces AWS Bedrock for these two mandatory-pack dimensions."
            ),
            active          = False,    # enable after model download + device config
            supported_dims  = ["prompt_injection", "jailbreak"],
            supported_roles = ["user_prompt"],
            extra_config   = {
                "model_path": "meta-llama/Prompt-Guard-86M",
                "device":     "cpu",    # or "cuda:0" for GPU
                "batch_size": 1,
            },
            # Probability thresholds: model output 0.0-1.0 -> canonical 1-9
            score_mapping = [
                {"range": [0.0, 0.30], "canonical": 1, "status": "PASS"},
                {"range": [0.30, 0.70], "canonical": 5, "status": "WARNING"},
                {"range": [0.70, 1.0],  "canonical": 8, "status": "FLAGGED"},
            ],
            category_mapping = {
                "INJECTION": "prompt_injection",
                "JAILBREAK": "jailbreak",
            },
            fail_mode        = "fail_open",
            health_status    = "healthy",
            created_at = _now(), updated_at = _now(),
        ),

        # ── LLM Judge ────────────────────────────────────────────────────────
        # LLM-as-judge for EU AI Act compliance dimensions that require
        # contextual reasoning (not achievable with keyword or coarse classifiers).
        # active=False — requires cloud API access or local model.
        # Configure model_name in extra_config when enabling.
        Provider(
            provider_id   = "llm_judge",
            provider_type = "llm_judge",
            display_name  = "LLM Judge",
            description   = (
                "LLM-as-judge evaluator for context-dependent compliance dimensions. "
                "Uses structured JSON prompts to score EU AI Act obligations "
                "(transparency, prohibited practices, bias). "
                "Model and prompt template configurable per deployment."
            ),
            active          = False,    # enable when cloud API or local LLM is configured
            supported_dims  = [
                "eu_ai_act_transparency",
                "eu_ai_act_prohibited_practice",
                "eu_ai_act_bias_discrimination",
            ],
            supported_roles = ["user_prompt", "llm_response", "tool_call", "agent_to_agent"],
            extra_config = {
                "model_name":          "claude-3-5-haiku-20241022",
                "api_key_env_var":     "ANTHROPIC_API_KEY",
                "temperature":         0,
                "max_tokens":          512,
                "prompt_template_id":  "eu_ai_act_v1",
            },
            score_mapping    = None,   # score returned inline in structured JSON response
            category_mapping = None,
            fail_mode        = "fail_open",
            health_status    = "healthy",
            created_at = _now(), updated_at = _now(),
        ),
    ]

    for p in providers:
        if p.provider_id in existing:
            continue
        db.add(p)
        count += 1

    if count:
        db.commit()
        logger.info(f"Seeded {count} provider(s) into config_providers")

    # ── Targeted correction: clear mock_provider capability filter ────────────
    # mock_provider is the universal fallback and must accept any dimension.
    # A previous seed version erroneously declared a restrictive supported_dims
    # list, which blocked assignment to dimensions like data_leakage.
    # This correction is safe to apply on every startup: mock_provider having
    # non-empty supported_dims is always a configuration bug, never intentional.
    mock = db.query(Provider).filter(Provider.provider_id == "mock_provider").first()
    if mock and mock.supported_dims:
        logger.info(
            "[seed] Correcting mock_provider supported_dims: "
            f"{mock.supported_dims!r} → [] (universal fallback)"
        )
        mock.supported_dims = []
        db.commit()

    return count


# ── Main entry point ───────────────────────────────────────────────────────────

def seed_all(db: Session) -> dict:
    """
    Run all seeders in dependency order.  Safe to call on every startup —
    existing rows are never overwritten.

    Returns a dict of {seeder_name: rows_inserted}.
    """
    results = {}
    results["dimensions"]         = seed_dimensions(db)
    results["threshold_profiles"] = seed_threshold_profiles(db)
    results["policy_packs"]       = seed_policy_packs(db)
    results["apps"]               = seed_apps(db)
    results["policies"]           = seed_policies(db)           # explicit YAML definitions win
    results["stub_policies"]      = seed_stub_policies(db)      # fill remaining gaps
    results["session_policy"]     = seed_session_policy(db)
    results["service_configs"]    = seed_service_configs(db)
    results["providers"]          = seed_providers(db)

    # ── App-specific seeders (idempotent) ────────────────────────────────────
    # Each app seeder registers its app, pack assignments, and agent topology.
    # New client apps add a call here (idempotent; existing apps are skipped).

    total = sum(results.values())
    if total:
        logger.info(f"Seed complete — {total} rows inserted across {len(results)} seeders")
    else:
        logger.debug("Seed complete — all tables already populated, nothing to insert")

    return results
