"""
Prompt Guard SML provider — Meta Llama Prompt Guard 86M.

A purpose-built small ML model (86M parameters) for detecting prompt injection
and jailbreak attempts.  Runs in-process on CPU — no external API calls,
no per-evaluation cost, no external dependency for the two most critical
mandatory-pack dimensions.

Model: meta-llama/Prompt-Guard-86M (HuggingFace)
  Input:  raw text (the prompt or agent message)
  Output: class probabilities for BENIGN | INJECTION | JAILBREAK

Score mapping (from config_providers, defaults below):
  probability < 0.30  → canonical 1,  PASS
  probability 0.30–0.70 → canonical 5, WARNING
  probability > 0.70  → canonical 8,  FLAGGED

The provider is registered as active=False in config_providers until the
model has been downloaded and the device has been configured.  Set
active=True via the PAP provider panel to enable it.

Dependencies (optional — GS fails open if not installed):
  pip install transformers torch
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from providers.base import TrustProvider

logger = logging.getLogger(__name__)

PROVIDER_ID = "prompt_guard"

# ── Hardcoded fallbacks (used when DB is unavailable) ─────────────────────────

_DEFAULT_SCORE_MAPPING: List[Dict] = [
    {"range": [0.0,  0.30], "canonical": 1, "status": "PASS"},
    {"range": [0.30, 0.70], "canonical": 5, "status": "WARNING"},
    {"range": [0.70, 1.01], "canonical": 8, "status": "FLAGGED"},
]

_DEFAULT_CATEGORY_MAPPING: Dict[str, str] = {
    "INJECTION": "prompt_injection",
    "JAILBREAK": "jailbreak",
}

_DEFAULT_MODEL_PATH = "meta-llama/Prompt-Guard-86M"
_DEFAULT_DEVICE     = "cpu"


def _apply_prob_mapping(probability: float, score_mapping: List[Dict]) -> Tuple[int, str]:
    """Map model output probability (0.0–1.0) to canonical score + status."""
    for entry in score_mapping:
        lo, hi = entry["range"]
        if lo <= probability < hi:
            return entry["canonical"], entry["status"]
    return 8, "FLAGGED"


class PromptGuardProvider(TrustProvider):
    """
    In-process SML provider wrapping Meta Llama Prompt Guard 86M.

    Lazy-loads the model on first _evaluate() call to avoid startup delay.
    Fails open (score=1/PASS) if transformers/torch are not installed.
    """

    def __init__(self):
        self._model      = None
        self._tokenizer  = None
        self._device     = _DEFAULT_DEVICE
        self._model_path = _DEFAULT_MODEL_PATH
        self._loaded     = False
        self._load_error: Optional[str] = None

        self._score_mapping    = _DEFAULT_SCORE_MAPPING
        self._category_mapping = _DEFAULT_CATEGORY_MAPPING

        # Load config from DB
        try:
            from core.database import query_provider_config
            cfg = query_provider_config(PROVIDER_ID)
            if cfg:
                if cfg.get("score_mapping"):
                    self._score_mapping = cfg["score_mapping"]
                if cfg.get("category_mapping"):
                    self._category_mapping = cfg["category_mapping"]
                extra = cfg.get("extra_config") or {}
                self._model_path = extra.get("model_path", _DEFAULT_MODEL_PATH)
                self._device     = extra.get("device",     _DEFAULT_DEVICE)
        except Exception as exc:
            logger.warning(f"PromptGuardProvider: DB config load failed ({exc}) — using defaults")

        self._supported_dims = set(self._category_mapping.values())
        logger.info(
            f"PromptGuardProvider configured "
            f"(model={self._model_path}, device={self._device}) — "
            f"model will be loaded on first evaluation call"
        )

    def name(self) -> str:
        return PROVIDER_ID

    # ── Lazy model loader ─────────────────────────────────────────────────────

    def _ensure_loaded(self) -> bool:
        """
        Load the model and tokenizer on first call.
        Returns True if loaded successfully, False if unavailable.
        """
        if self._loaded:
            return self._model is not None

        self._loaded = True
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch

            logger.info(f"PromptGuardProvider: loading model '{self._model_path}' ...")
            t0 = time.time()
            self._tokenizer = AutoTokenizer.from_pretrained(self._model_path)
            self._model     = AutoModelForSequenceClassification.from_pretrained(
                self._model_path
            )
            self._model.eval()
            elapsed = round(time.time() - t0, 1)
            logger.info(f"PromptGuardProvider: model loaded in {elapsed}s on {self._device}")
            return True

        except ImportError as exc:
            self._load_error = (
                "transformers / torch not installed — "
                f"install them to use PromptGuardProvider: {exc}"
            )
            logger.warning(f"PromptGuardProvider: {self._load_error}")
            return False

        except Exception as exc:
            self._load_error = f"Model load failed: {exc}"
            logger.error(f"PromptGuardProvider: {self._load_error}", exc_info=True)
            return False

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score_text(self, content: str) -> Dict[str, float]:
        """
        Run inference and return per-class probabilities.
        Returns {"INJECTION": float, "JAILBREAK": float, "BENIGN": float}.
        """
        import torch
        inputs = self._tokenizer(
            content,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=True,
        )
        with torch.no_grad():
            logits = self._model(**inputs).logits
            probs  = torch.softmax(logits, dim=-1).squeeze().tolist()

        # Prompt Guard label order: BENIGN=0, INJECTION=1, JAILBREAK=2
        # (verify with model card if updated)
        id2label = getattr(self._model.config, "id2label", {
            0: "BENIGN", 1: "INJECTION", 2: "JAILBREAK"
        })
        if isinstance(probs, float):
            probs = [probs]

        return {id2label.get(i, str(i)).upper(): p for i, p in enumerate(probs)}

    # ── SPI implementation ────────────────────────────────────────────────────

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start = time.time()

        if not self._ensure_loaded():
            # Model unavailable — return PASS with informational note
            # (base class fail-open handles true errors; this is a soft unavailability)
            latency_ms = int((time.time() - start) * 1000)
            note = self._load_error or "Model not available"
            return [
                {
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note":         note,
                        "model_unavailable": True,
                    },
                }
                for dim in evaluations
                if dim in self._supported_dims
            ] + [
                {
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note": f"'{dim}' not supported by PromptGuardProvider"
                    },
                }
                for dim in evaluations
                if dim not in self._supported_dims
            ], latency_ms

        # Run model inference
        class_probs = self._score_text(content)
        latency_ms  = int((time.time() - start) * 1000)

        results = []
        for dim in evaluations:
            if dim not in self._supported_dims:
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "note": f"'{dim}' is not supported by PromptGuardProvider."
                    },
                })
                continue

            # Find the model class that maps to this canonical dimension
            model_class = next(
                (k for k, v in self._category_mapping.items() if v == dim),
                None,
            )
            if model_class and model_class in class_probs:
                prob = class_probs[model_class]
            else:
                # Fallback: use max non-BENIGN probability
                prob = max(
                    (v for k, v in class_probs.items() if k != "BENIGN"),
                    default=0.0,
                )

            score, status = _apply_prob_mapping(prob, self._score_mapping)
            results.append({
                "dimension": dim,
                "score":     score,
                "status":    status,
                "provider_metadata": {
                    "model":       self._model_path,
                    "model_class": model_class,
                    "probability": round(prob, 4),
                    "all_probs":   {k: round(v, 4) for k, v in class_probs.items()},
                },
            })

        return results, latency_ms
