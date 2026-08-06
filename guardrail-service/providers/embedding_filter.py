"""
EmbeddingFilter — semantic topic & repetition screening using all-MiniLM-L6-v2.

Design
------
This provider evaluates two dimensions:

  off_topic_request   — cosine similarity between the incoming prompt and a set
                        of "allowed topic" seed phrases.  Low similarity = off-topic.

  prompt_repetition   — session-relative.  The GS is stateless, so this evaluator
                        is NOT scored here — the Policy Engine owns session state and
                        injects the repetition score directly from PromptDeduplicator
                        (policy-engine/session/dedup.py).  This provider returns
                        score=1 (PASS) for prompt_repetition as a no-op placeholder
                        so it satisfies the evaluation registry contract.

Config load order:
  1. DB: config_service_configs('gs', 'embedding_filter') — preferred
  2. YAML: config/embedding_filter.yaml — legacy fallback

Model
-----
all-MiniLM-L6-v2 (sentence-transformers):
  - Size:     ~22 MB
  - Dims:     384
  - Latency:  10–15 ms on CPU
  - Memory:   ~80 MB resident

Fail-open behaviour
-------------------
If sentence-transformers is not installed (or the model download fails), the
provider falls back to score=1 (PASS) for all dimensions with a UNKNOWN status.
This ensures the platform stays operational without blocking evaluations if the
optional ML dependency is missing.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import yaml

from providers.base import TrustProvider

logger = logging.getLogger(__name__)

# ── Optional ML dependency (fails open) ──────────────────────────────────────

try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    _SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    _SENTENCE_TRANSFORMERS_AVAILABLE = False
    logger.info(
        "[EmbeddingFilter] sentence-transformers not installed — "
        "EmbeddingFilter will fail open (score=1 for all dimensions). "
        "Install with: pip install sentence-transformers"
    )

# ── Default allowed topics (override via DB or config/embedding_filter.yaml) ──

_DEFAULT_ALLOWED_TOPICS = [
    "banking and financial services",
    "account management and transactions",
    "loan and mortgage queries",
    "investment and savings advice",
    "customer service and support",
    "fraud and security alerts",
    "payments and transfers",
    "credit card and debit card",
    "interest rates and fees",
    "regulatory compliance and reporting",
]

_DEFAULT_OFF_TOPIC_THRESHOLD  = 0.35
_DEFAULT_NEAR_TOPIC_THRESHOLD = 0.50
_DEFAULT_CONFIG_PATH          = "config/embedding_filter.yaml"
_MODEL_NAME                   = "all-MiniLM-L6-v2"


class EmbeddingFilterProvider(TrustProvider):
    """
    Semantic topic filter using cosine similarity.

    Lazily loads the embedding model on first evaluate() call so service
    startup is not blocked by the model download / load time.
    """

    def __init__(self, config_path: str = _DEFAULT_CONFIG_PATH):
        self._config_path         = config_path
        self._allowed_topics:     List[str] = list(_DEFAULT_ALLOWED_TOPICS)
        self._off_topic_threshold: float    = _DEFAULT_OFF_TOPIC_THRESHOLD
        self._near_threshold:      float    = _DEFAULT_NEAR_TOPIC_THRESHOLD
        self._model: Optional["SentenceTransformer"] = None
        self._topic_embeddings: Optional["np.ndarray"] = None
        self._load_config()

    # ── Config ──────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        """Try DB first; fall back to YAML file."""
        if not self._load_config_from_db():
            self._load_config_from_yaml()

    def _load_config_from_db(self) -> bool:
        """
        Load config from config_service_configs('gs', 'embedding_filter').
        Returns True on success, False if DB unavailable or blob absent.
        """
        try:
            from core.database import query_service_config, GS_DB_AVAILABLE
            if not GS_DB_AVAILABLE:
                return False

            cfg = query_service_config("gs", "embedding_filter")
            if not cfg:
                return False

            self._apply_config(cfg)
            logger.info(
                f"[EmbeddingFilter] Loaded from PAP DB — "
                f"{len(self._allowed_topics)} topics, "
                f"off_topic_threshold={self._off_topic_threshold}"
            )
            return True

        except Exception as exc:
            logger.warning(f"[EmbeddingFilter] DB load failed ({exc}) — will try YAML")
            return False

    def _load_config_from_yaml(self) -> None:
        """Load config from YAML file (legacy fallback)."""
        try:
            with open(self._config_path) as f:
                cfg = yaml.safe_load(f) or {}
            self._apply_config(cfg)
            logger.info(
                f"[EmbeddingFilter] Loaded from YAML {self._config_path} — "
                f"{len(self._allowed_topics)} topics, "
                f"off_topic_threshold={self._off_topic_threshold}"
            )
        except FileNotFoundError:
            logger.info(
                f"[EmbeddingFilter] {self._config_path} not found — using defaults "
                f"({len(self._allowed_topics)} topics)"
            )
        except Exception as exc:
            logger.warning(f"[EmbeddingFilter] Error loading config: {exc} — using defaults")

    def _apply_config(self, cfg: dict) -> None:
        """Apply a parsed config dict (from DB blob or YAML) to internal state."""
        topics = cfg.get("allowed_topics", [])
        if topics:
            self._allowed_topics = topics
        self._off_topic_threshold = float(
            cfg.get("off_topic_threshold", _DEFAULT_OFF_TOPIC_THRESHOLD)
        )
        self._near_threshold = float(
            cfg.get("near_topic_threshold", _DEFAULT_NEAR_TOPIC_THRESHOLD)
        )
        # Invalidate cached topic embeddings so they are re-computed on next call
        self._topic_embeddings = None

    def reload(self) -> None:
        """Hot-reload config — tries DB first, falls back to YAML."""
        self._allowed_topics      = list(_DEFAULT_ALLOWED_TOPICS)
        self._off_topic_threshold = _DEFAULT_OFF_TOPIC_THRESHOLD
        self._near_threshold      = _DEFAULT_NEAR_TOPIC_THRESHOLD
        self._load_config()

    # ── Model lazy-loading ───────────────────────────────────────────────────

    def _ensure_model(self) -> bool:
        """Load model on first call.  Returns False if unavailable."""
        if self._model is not None:
            return True
        if not _SENTENCE_TRANSFORMERS_AVAILABLE:
            return False
        try:
            logger.info(f"[EmbeddingFilter] Loading model {_MODEL_NAME}…")
            self._model = SentenceTransformer(_MODEL_NAME)
            logger.info(f"[EmbeddingFilter] Model {_MODEL_NAME} loaded")
            return True
        except Exception as exc:
            logger.error(f"[EmbeddingFilter] Failed to load model: {exc}")
            return False

    def _ensure_topic_embeddings(self) -> bool:
        """Pre-compute topic embeddings (cached until config reload)."""
        if self._topic_embeddings is not None:
            return True
        if not self._ensure_model():
            return False
        try:
            self._topic_embeddings = self._model.encode(
                self._allowed_topics,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            return True
        except Exception as exc:
            logger.error(f"[EmbeddingFilter] Failed to encode topics: {exc}")
            return False

    # ── Scoring ──────────────────────────────────────────────────────────────

    def _score_off_topic(self, content: str) -> Tuple[int, Dict]:
        """
        Returns (score, metadata_dict).
        Score:
          1 — PASS (on-topic)
          5 — NORMATIVE (borderline)
          8 — COERCIVE (off-topic)
          1 — PASS / fail-open (model unavailable)
        """
        if not self._ensure_topic_embeddings():
            return 1, {"model": "unavailable", "fail_open": True}

        try:
            prompt_emb = self._model.encode(
                content,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            # Cosine similarities (embeddings are unit-norm, so dot product = cosine sim)
            sims       = (self._topic_embeddings @ prompt_emb).tolist()
            max_sim    = float(max(sims))
            best_topic = self._allowed_topics[sims.index(max_sim)]

            meta = {
                "model":          _MODEL_NAME,
                "max_similarity": round(max_sim, 4),
                "best_topic":     best_topic,
                "embedding":      prompt_emb.tolist(),  # returned so PE can store for dedup
            }

            if max_sim >= self._near_threshold:
                return 1, meta      # on-topic
            elif max_sim >= self._off_topic_threshold:
                return 5, meta      # borderline
            else:
                return 8, meta      # off-topic
        except Exception as exc:
            logger.warning(f"[EmbeddingFilter] Scoring error: {exc} — fail open")
            return 1, {"model": _MODEL_NAME, "error": str(exc), "fail_open": True}

    # ── TrustProvider interface ──────────────────────────────────────────────

    def name(self) -> str:
        return "embedding_filter"

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start   = time.time()
        results = []

        for dim in evaluations:
            if dim == "off_topic_request":
                score, meta = self._score_off_topic(content)
                status = "PASS" if score <= 3 else "WARNING" if score <= 6 else "FLAGGED"
                results.append({
                    "dimension":         dim,
                    "score":             score,
                    "status":            status,
                    "provider_metadata": {"provider": "embedding_filter", **meta},
                })

            elif dim == "prompt_repetition":
                # Stateless placeholder — PE injects real score from PromptDeduplicator.
                # Return PASS so downstream policy logic isn't confused by a missing score.
                results.append({
                    "dimension":         dim,
                    "score":             1,
                    "status":            "PASS",
                    "provider_metadata": {
                        "provider": "embedding_filter",
                        "note":     "session-relative score injected by Policy Engine",
                    },
                })

            else:
                # Unknown dimension — return UNKNOWN
                results.append({
                    "dimension":         dim,
                    "score":             None,
                    "status":            "UNKNOWN",
                    "provider_metadata": {
                        "provider": "embedding_filter",
                        "note":     f"dimension '{dim}' not handled by EmbeddingFilter",
                    },
                })

        latency_ms = max(1, int((time.time() - start) * 1000))
        return results, latency_ms
