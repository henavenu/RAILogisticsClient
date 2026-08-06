"""
AWS Bedrock Guardrail provider adapter.

Supported dimensions (active):
  misconduct, sensitive_data_disclosure, sensitive_data_request,
  prompt_injection, jailbreak, malicious_intent

Score and category mappings are loaded from config_providers at init time
(DB-first, hardcoded fallback).  Operators can update mappings via the PAP
without redeploying the GS.

IMPORTANT: Guardrail must be configured in Detect Mode (inputAction/outputAction = NONE)
           so it returns assessments for all content, not just blocked content.
"""
import json
import os
import time
import logging
from typing import Dict, List, Optional, Tuple

from providers.base import TrustProvider

logger = logging.getLogger(__name__)

# ── Hardcoded fallbacks (used when DB is unavailable) ─────────────────────────

_DEFAULT_CATEGORY_MAPPING: Dict[str, str] = {
    "MISCONDUCT":       "misconduct",
    "HATE":             "hate",
    "SEXUAL":           "sexual_content",
    "VIOLENCE":         "violence",
    "SELF_HARM":        "self_harm",
    "HARASSMENT":       "harassment",
    "PROMPT_ATTACK":    "prompt_injection",
    "PROMPT_INJECTION": "prompt_injection",
    "JAILBREAK":        "jailbreak",
}

_DEFAULT_SCORE_MAPPING: Dict = {
    "type": "categorical",
    "map": {
        "CRITICAL": {"canonical": 9, "status": "FLAGGED"},
        "HIGH":     {"canonical": 8, "status": "FLAGGED"},
        "MEDIUM":   {"canonical": 5, "status": "WARNING"},
        "LOW":      {"canonical": 2, "status": "PASS"},
    },
}

# Dimensions that only apply to content the model *outputs* (llm_response / tool_result).
# Evaluating them on user_prompt or tool_call content causes false positives — e.g. dollar
# amounts in "Running session total: $200.00" trip Bedrock's PII detection and produce a
# spurious TP-PV-001 · sensitive_data_disclosure block on an innocent domestic transfer.
# Mirror of mock_provider._RESPONSE_ONLY_DIMENSIONS.
_RESPONSE_ONLY_DIMENSIONS = frozenset({"sensitive_data_disclosure", "data_minimisation_violation"})


def _apply_confidence_mapping(
    confidence: str,
    score_mapping: Dict,
) -> Tuple[int, str]:
    """Map Bedrock confidence string to canonical score (1-9) and status."""
    c = (confidence or "").upper()
    mapping = score_mapping.get("map", {})
    entry = mapping.get(c)
    if entry:
        return entry["canonical"], entry["status"]
    # Unknown confidence — treat as LOW/PASS
    return 2, "PASS"


class AWSBedrockProvider(TrustProvider):
    """
    Adapter for AWS Bedrock Guardrails API.

    Reads credentials from env vars whose NAMES are stored in config_providers:
      AWS_ACCESS_KEY_ID, AWS_SECRET_KEY
    Reads guardrail ARN from env var name stored in extra_config.
    Score and category mappings loaded from config_providers at init.
    """

    def __init__(
        self,
        guardrail_arn: Optional[str] = None,
        region_name: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        guardrail_version: str = "DRAFT",
    ):
        # ── Load config from DB ───────────────────────────────────────────────
        self._score_mapping    = _DEFAULT_SCORE_MAPPING
        self._category_mapping = _DEFAULT_CATEGORY_MAPPING
        self._fail_mode        = "fail_open"

        try:
            from core.database import query_provider_config
            cfg = query_provider_config("aws_bedrock_guardrail")
            if cfg:
                if cfg.get("score_mapping"):
                    self._score_mapping = cfg["score_mapping"]
                    logger.debug("AWSProvider: loaded score_mapping from DB")
                if cfg.get("category_mapping"):
                    self._category_mapping = cfg["category_mapping"]
                    logger.debug("AWSProvider: loaded category_mapping from DB")

                extra = cfg.get("extra_config") or {}
                if aws_access_key_id is None and cfg.get("api_key_env_var"):
                    aws_access_key_id = os.getenv(cfg["api_key_env_var"], "").strip() or None
                if aws_secret_access_key is None:
                    aws_secret_access_key = (
                        os.getenv(extra.get("aws_secret_env_var", "AWS_SECRET_KEY"), "").strip()
                        or None
                    )
                if guardrail_arn is None:
                    guardrail_arn = (
                        os.getenv(extra.get("guardrail_arn_env_var", "AWS_BEDROCK_GUARDRAIL_ARN"), "").strip()
                        or None
                    )
                if region_name is None:
                    region_name = extra.get("region", "us-east-2")
                if guardrail_version == "DRAFT":
                    guardrail_version = str(extra.get("guardrail_version", "DRAFT"))
                if cfg.get("fail_mode"):
                    self._fail_mode = cfg["fail_mode"]
        except Exception as exc:
            logger.warning(f"AWSProvider: DB config load failed ({exc}) — using defaults")

        self._supported_dims = set(self._category_mapping.values()) | {"sensitive_data_disclosure"}

        # Resolve remaining params from env if not provided
        aws_access_key_id     = aws_access_key_id     or os.getenv("AWS_ACCESS_KEY_ID", "").strip() or None
        aws_secret_access_key = aws_secret_access_key or os.getenv("AWS_SECRET_KEY", "").strip()    or None
        guardrail_arn         = guardrail_arn         or os.getenv("AWS_BEDROCK_GUARDRAIL_ARN", "").strip() or None
        region_name           = region_name           or "us-east-2"

        self.guardrail_arn     = guardrail_arn     or ""
        self.guardrail_version = guardrail_version
        self.region_name       = region_name

        if not guardrail_arn or not aws_access_key_id:
            logger.warning(
                "★ AWSBedrockProvider: AWS_ACCESS_KEY_ID or AWS_BEDROCK_GUARDRAIL_ARN not set — "
                f"all assigned dimensions will {self._fail_mode.upper().replace('_',' ')} "
                "(use MOCK_MODE=true in dev to route to mock_provider instead)."
            )
            self._client = None
        else:
            try:
                import boto3
                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=region_name,
                    aws_access_key_id=aws_access_key_id,
                    aws_secret_access_key=aws_secret_access_key,
                )
                logger.info(
                    f"AWSBedrockProvider initialised "
                    f"(ARN: {guardrail_arn}, Version: {guardrail_version})"
                )
            except ImportError:
                logger.warning("boto3 not installed — install it to use the AWS provider.")
                self._client = None

    def name(self) -> str:
        return "aws_bedrock_guardrail"

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start = time.time()

        if self._client is None:
            raise RuntimeError(
                "AWS Bedrock client not initialised — "
                "check AWS_ACCESS_KEY_ID, AWS_SECRET_KEY, AWS_BEDROCK_GUARDRAIL_ARN"
            )

        # ── Role gate: skip response-only dimensions for non-output roles ─────
        # sensitive_data_disclosure / data_minimisation_violation are only valid
        # for content the model is *producing*.  Dollar amounts, account numbers,
        # etc. in tool_call / user_prompt content would otherwise trigger Bedrock
        # PII detection → false TP-PV-001 blocks on innocent requests.
        if content_role not in ("llm_response", "tool_result"):
            role_skipped = _RESPONSE_ONLY_DIMENSIONS & set(evaluations)
            active_evals = [d for d in evaluations if d not in role_skipped]
        else:
            role_skipped  = frozenset()
            active_evals  = list(evaluations)

        if role_skipped:
            logger.debug(
                f"AWSProvider: role-gating {role_skipped} for content_role={content_role!r} "
                f"— returning PASS without calling Bedrock for those dimensions"
            )

        # If every requested dimension was role-gated out, skip the API call entirely
        if not active_evals:
            latency_ms = int((time.time() - start) * 1000)
            return [
                {"dimension": d, "score": 1, "status": "PASS",
                 "provider_metadata": {"note": f"response-only dimension, skipped for role={content_role}"}}
                for d in evaluations
            ], latency_ms

        source = "OUTPUT" if content_role == "llm_response" else "INPUT"

        from botocore.exceptions import ClientError
        response = self._client.apply_guardrail(
            guardrailIdentifier=self.guardrail_arn,
            guardrailVersion=self.guardrail_version,
            source=source,
            content=[{"text": {"text": content}}],
        )
        logger.debug(f"Bedrock raw response keys: {list(response.keys())}")

        found: Dict[str, dict] = {}
        action = response.get("action", "")

        # ── Parse assessments ─────────────────────────────────────────────────
        policy_results = []
        for assessment in response.get("assessments", []):
            if "policyResults" in assessment:
                policy_results.extend(assessment["policyResults"])
        policy_results.extend(response.get("policyResults", []))

        for pr in policy_results:
            # PII / sensitive information → sensitive_data_disclosure
            if "sensitiveInformationPolicy" in pr and "sensitive_data_disclosure" in active_evals:
                pii_entities = pr["sensitiveInformationPolicy"].get("piiEntities", [])
                if pii_entities and "sensitive_data_disclosure" not in found:
                    pii_action = pii_entities[0].get("action", "NONE")
                    score, status = (
                        (8, "FLAGGED") if pii_action in ("BLOCK", "ANONYMIZE")
                        else (5, "WARNING")
                    )
                    found["sensitive_data_disclosure"] = {
                        "dimension": "sensitive_data_disclosure",
                        "score":     score,
                        "status":    status,
                        "provider_metadata": {
                            "bedrock_category": "SENSITIVE_INFORMATION",
                            "pii_type":         pii_entities[0].get("type"),
                            "bedrock_action":   pii_action,
                            "guardrail_action": action,
                        },
                    }
                continue

            # Content policy filters
            filters = []
            if "contentPolicy" in pr:
                filters.extend(pr["contentPolicy"].get("filters", []))
            if "type" in pr or "category" in pr:
                filters.append(pr)

            for f in filters:
                category   = (f.get("category") or f.get("type") or "").upper()
                canonical  = self._category_mapping.get(category)
                if not canonical or canonical not in active_evals or canonical in found:
                    continue
                confidence = (f.get("confidence") or f.get("severity") or "LOW")
                score, status = _apply_confidence_mapping(confidence, self._score_mapping)
                found[canonical] = {
                    "dimension": canonical,
                    "score":     score,
                    "status":    status,
                    "provider_metadata": {
                        "bedrock_category":   category,
                        "bedrock_confidence": confidence,
                        "bedrock_action":     f.get("action"),
                        "guardrail_action":   action,
                    },
                }

        # ── Build final results — one per requested dimension ─────────────────
        results = []
        for dim in evaluations:
            if dim in role_skipped:
                # Role-gated: dimension is out of scope for this content_role
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note":           f"response-only dimension, skipped for role={content_role}",
                        "guardrail_action": "NONE",
                    },
                })
            elif dim in found:
                results.append(found[dim])
            elif dim in self._supported_dims:
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note":            "No violation detected by Bedrock",
                        "guardrail_action": action,
                    },
                })
            else:
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note": f"'{dim}' is not supported by AWS Bedrock Guardrail."
                    },
                })

        latency_ms = int((time.time() - start) * 1000)
        return results, latency_ms
