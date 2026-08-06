"""
Azure Content Safety provider adapter.

Supported dimensions (active):
  hate, harassment, sexual_content, self_harm, violence

Score and category mappings are loaded from config_providers at init time
(DB-first, hardcoded fallback).  This means operators can update mappings
via the PAP without redeploying the GS.

All other requested dimensions receive a default PASS with a metadata note.
"""
import os
import time
import logging
from typing import Dict, List, Optional, Tuple

from providers.base import TrustProvider

logger = logging.getLogger(__name__)

# ── Hardcoded fallbacks (used when DB is unavailable) ─────────────────────────

_DEFAULT_CATEGORY_MAPPING: Dict[str, str] = {
    "Hate":       "hate",
    "Sexual":     "sexual_content",
    "Violence":   "violence",
    "SelfHarm":   "self_harm",
    "Harassment": "harassment",
}

# Sorted ascending by range[1] for sequential lookup
_DEFAULT_SCORE_MAPPING: List[Dict] = [
    {"range": [0, 1], "canonical": 1, "status": "PASS"},
    {"range": [2, 3], "canonical": 3, "status": "PASS"},
    {"range": [4, 5], "canonical": 5, "status": "WARNING"},
    {"range": [6, 7], "canonical": 8, "status": "FLAGGED"},
]


def _apply_score_mapping(
    severity: int,
    score_mapping: List[Dict],
) -> Tuple[int, str]:
    """Map Azure severity (0-7) to canonical score (1-9) and status label."""
    for entry in score_mapping:
        lo, hi = entry["range"]
        if lo <= severity <= hi:
            return entry["canonical"], entry["status"]
    # Fallback for any out-of-range value
    return 9, "FLAGGED"


class AzureContentSafetyProvider(TrustProvider):
    """
    Adapter for the Azure Content Safety REST API.

    Reads endpoint from the AZURE_ENDPOINT env var (name stored in DB row).
    Reads api_key from the AZURE_API_KEY env var (name stored in DB row).
    Score and category mappings are loaded from config_providers at init.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        # ── Load config from DB (score/category mappings) ─────────────────────
        self._score_mapping    = _DEFAULT_SCORE_MAPPING
        self._category_mapping = _DEFAULT_CATEGORY_MAPPING
        self._fail_mode        = "fail_open"

        try:
            from core.database import query_provider_config
            cfg = query_provider_config("azure_content_safety")
            if cfg:
                if cfg.get("score_mapping"):
                    self._score_mapping = cfg["score_mapping"]
                    logger.debug("AzureProvider: loaded score_mapping from DB")
                if cfg.get("category_mapping"):
                    self._category_mapping = cfg["category_mapping"]
                    logger.debug("AzureProvider: loaded category_mapping from DB")
                # Resolve endpoint/api_key from DB-stored env var names if not passed
                if endpoint is None and cfg.get("extra_config"):
                    env_var = cfg["extra_config"].get("endpoint_env_var", "AZURE_ENDPOINT")
                    endpoint = os.getenv(env_var, "").strip() or endpoint
                if api_key is None and cfg.get("api_key_env_var"):
                    api_key = os.getenv(cfg["api_key_env_var"], "").strip() or api_key
                if cfg.get("fail_mode"):
                    self._fail_mode = cfg["fail_mode"]
        except Exception as exc:
            logger.warning(f"AzureProvider: DB config load failed ({exc}) — using defaults")

        # Build reverse lookup: canonical dim -> Azure category name
        self._canonical_to_azure: Dict[str, str] = {
            v: k for k, v in self._category_mapping.items()
        }
        self._supported_dims = set(self._category_mapping.values())

        # ── Initialise Azure client ───────────────────────────────────────────
        endpoint = (endpoint or os.getenv("AZURE_ENDPOINT", "")).rstrip("/")
        api_key  = api_key  or os.getenv("AZURE_API_KEY", "")

        if not endpoint or not api_key:
            logger.warning(
                "★ AzureContentSafetyProvider: AZURE_ENDPOINT or AZURE_API_KEY not set — "
                f"all assigned dimensions will {self._fail_mode.upper().replace('_',' ')} "
                "(use MOCK_MODE=true in dev to route to mock_provider instead)."
            )
            self._client = None
        else:
            try:
                from azure.ai.contentsafety import ContentSafetyClient
                from azure.ai.contentsafety.models import AnalyzeTextOptions  # noqa: F401
                from azure.core.credentials import AzureKeyCredential
                self._client = ContentSafetyClient(
                    endpoint=endpoint,
                    credential=AzureKeyCredential(api_key),
                )
                logger.info("AzureContentSafetyProvider initialised")
            except ImportError:
                logger.warning(
                    "azure-ai-contentsafety package not installed — "
                    "install it to use the Azure provider."
                )
                self._client = None

    def name(self) -> str:
        return "azure_content_safety"

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start = time.time()

        if self._client is None:
            latency_ms = int((time.time() - start) * 1000)
            raise RuntimeError(
                "Azure client not initialised — check AZURE_ENDPOINT and AZURE_API_KEY"
            )

        from azure.ai.contentsafety.models import AnalyzeTextOptions
        response = self._client.analyze_text(AnalyzeTextOptions(text=content))

        # Build lookup: canonical_dim -> {score, status, azure_severity}
        azure_results: Dict[str, dict] = {}
        for cat in response.categories_analysis or []:
            canonical = self._category_mapping.get(str(cat.category))
            if not canonical:
                continue
            score, status = _apply_score_mapping(cat.severity, self._score_mapping)
            azure_results[canonical] = {
                "score":  score,
                "status": status,
                "provider_metadata": {
                    "azure_category": str(cat.category),
                    "azure_severity": cat.severity,
                },
            }

        results = []
        for dim in evaluations:
            if dim in self._supported_dims:
                if dim in azure_results:
                    r = azure_results[dim]
                    results.append({
                        "dimension":         dim,
                        "score":             r["score"],
                        "status":            r["status"],
                        "provider_metadata": r["provider_metadata"],
                    })
                else:
                    # Azure always returns all categories; absence = severity 0
                    results.append({
                        "dimension": dim,
                        "score":     1,
                        "status":    "PASS",
                        "provider_metadata": {
                            "azure_category": self._canonical_to_azure.get(dim),
                            "azure_severity": 0,
                            "note": "Azure returned no category entry — severity=0 (safe)",
                        },
                    })
            else:
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note": (
                            f"'{dim}' is not supported by Azure Content Safety. "
                            "Returning default PASS. Route to a capable provider."
                        )
                    },
                })

        latency_ms = int((time.time() - start) * 1000)
        return results, latency_ms
