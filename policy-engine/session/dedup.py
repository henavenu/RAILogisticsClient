"""
PromptDeduplicator — per-session prompt deduplication for the Policy Engine.

Two detection layers
--------------------
1. Exact (SHA-256 hash)
   Zero false positives.  If the SHA-256 of the current prompt matches any
   previous hash in the session window, it is an exact duplicate.
   Cost: ~0 ms (single dict lookup).

2. Near-duplicate (cosine similarity, all-MiniLM-L6-v2)
   Catches rephrased repeats: "What's my balance?" / "Tell me my balance"
   / "Can you check my balance?" all embed very similarly.
   Cost: 10–15 ms (model inference) + O(window) dot products.
   Requires sentence-transformers; falls back to exact-only if unavailable.

Integration
-----------
The PE orchestrator calls `check_and_record()` before dispatching to the
Guardrail Service for every user_prompt turn.

Return value:
  DedupResult(
      is_exact_duplicate     bool   — SHA-256 matched a previous prompt
      is_near_duplicate      bool   — cosine similarity ≥ threshold
      similarity             float  — highest cosine sim found (0.0 if no embedding)
      repetition_score       int    — score to inject into prompt_repetition evaluator
                                      1 = no repeat, 5 = near-dup, 8 = exact dup
  )

Session state additions (see store.py SessionState)
----------------------------------------------------
  prompt_hashes:     List[str]        SHA-256 hex digests (most recent last)
  prompt_embeddings: List[List[float]] 384-dim unit-norm vectors (most recent last)
  These lists are bounded by SESSION_DEDUP_WINDOW from session_policy.yaml.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Optional ML dependency ────────────────────────────────────────────────────
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    _ST_AVAILABLE = True
except ImportError:
    _ST_AVAILABLE = False
    logger.info(
        "[PromptDedup] sentence-transformers not installed — "
        "falling back to exact-match deduplication only."
    )

_MODEL_NAME = "all-MiniLM-L6-v2"
_model: Optional["SentenceTransformer"] = None


def _get_model() -> Optional["SentenceTransformer"]:
    """Lazy-load the embedding model (singleton)."""
    global _model
    if _model is not None:
        return _model
    if not _ST_AVAILABLE:
        return None
    try:
        logger.info(f"[PromptDedup] Loading embedding model {_MODEL_NAME}…")
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info(f"[PromptDedup] Model {_MODEL_NAME} ready")
        return _model
    except Exception as exc:
        logger.error(f"[PromptDedup] Failed to load {_MODEL_NAME}: {exc}")
        return None


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class DedupResult:
    is_exact_duplicate: bool
    is_near_duplicate:  bool
    similarity:         float   # highest cosine sim (0.0 if embedding unavailable)
    repetition_score:   int     # 1 = no repeat, 3 = near-dup, 5 = exact dup
                                # Intentionally kept in PERMISSIVE range (1-9 scale,
                                # permissive threshold now covers 1-9) — observability
                                # only; no enforcement action taken on this dimension.


# ── Public API ────────────────────────────────────────────────────────────────

def check_and_record(
    prompt:         str,
    hashes:         List[str],
    embeddings:     List[List[float]],
    threshold:      float = 0.92,
    window:         int   = 50,
) -> DedupResult:
    """
    Check prompt against session history and record it.

    Mutates `hashes` and `embeddings` in-place (both are lists stored in
    SessionState — the caller is responsible for persisting SessionState after
    this call returns).

    Parameters
    ----------
    prompt      The raw prompt text.
    hashes      Mutable list of previous SHA-256 digests (oldest first).
    embeddings  Mutable list of previous 384-dim embeddings (oldest first).
    threshold   Cosine similarity above which a prompt is near-duplicate.
    window      Maximum number of previous items to retain.

    Returns
    -------
    DedupResult
    """
    # ── Layer 1: exact SHA-256 match ─────────────────────────────────────────
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if digest in hashes:
        # Still record the new occurrence so the window stays current
        _append_bounded(hashes, digest, window)
        _append_bounded(embeddings, [], window)   # placeholder — no new embedding
        logger.debug(f"[PromptDedup] Exact duplicate detected (sha256={digest[:8]}…)")
        return DedupResult(
            is_exact_duplicate=True,
            is_near_duplicate=False,
            similarity=1.0,
            repetition_score=5,   # Observability score — PERMISSIVE range (policy threshold 1-9)
        )

    # ── Layer 2: cosine similarity (near-duplicate) ───────────────────────────
    max_sim  = 0.0
    new_emb: List[float] = []

    model = _get_model()
    if model is not None:
        try:
            prompt_emb = model.encode(
                prompt,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            new_emb = prompt_emb.tolist()

            # Only compare against embeddings that were actually stored
            valid_embs = [e for e in embeddings if e]
            if valid_embs:
                history = np.array(valid_embs, dtype="float32")
                sims    = (history @ prompt_emb).tolist()
                max_sim = float(max(sims))
        except Exception as exc:
            logger.warning(f"[PromptDedup] Embedding comparison failed: {exc}")

    # Record this prompt
    _append_bounded(hashes, digest, window)
    _append_bounded(embeddings, new_emb, window)

    is_near = max_sim >= threshold
    if is_near:
        logger.debug(
            f"[PromptDedup] Near-duplicate (sim={max_sim:.3f} ≥ {threshold}) "
            f"sha256={digest[:8]}…"
        )
    return DedupResult(
        is_exact_duplicate=False,
        is_near_duplicate=is_near,
        similarity=round(max_sim, 4),
        repetition_score=3 if is_near else 1,   # 3=near-dup, 1=unique; PERMISSIVE range
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _append_bounded(lst: list, item, max_size: int) -> None:
    """Append item to list, trimming oldest entries if length exceeds max_size."""
    lst.append(item)
    while len(lst) > max_size:
        lst.pop(0)
