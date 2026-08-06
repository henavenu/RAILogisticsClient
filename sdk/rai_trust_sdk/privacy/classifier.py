"""
TopicClassifier — local zero-shot topic relevance classifier.

Uses sentence-transformers (all-MiniLM-L6-v2, 22 MB) to embed the incoming
prompt and compare it against on-domain and off-topic anchor sentences.
No network calls — runs entirely in-process.

If sentence-transformers is not installed, all calls to is_off_topic()
return False (fail-open) and a warning is logged once.
"""
from __future__ import annotations

import logging
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# ── Anchor sentences ──────────────────────────────────────────────────────────
# These define the boundary between "work" and "not work".
# The caller's domain_description is appended to _ON_TOPIC at runtime so
# the classifier is aware of the specific application's purpose.

_OFF_TOPIC: List[str] = [
    "Write me a poem or a short story",
    "Help me plan my vacation or book travel",
    "Give me a recipe or cooking instructions",
    "Tell me a joke or something funny",
    "Help with my personal social media posts",
    "Write personal emails or messages for me",
    "Recommend movies, TV shows, or music",
    "Help me with personal shopping",
    "Play a game with me",
    "Write my dating profile or personal bio",
]

_ON_TOPIC_BASE: List[str] = [
    "Help me with software development and coding",
    "Debug this error or exception",
    "Review or refactor this code",
    "Explain this technical concept or architecture",
    "Write or improve unit tests",
    "Help with API design or integration",
    "Analyse data or write a query",
    "Review a pull request or code change",
    "Help with system design or infrastructure",
    "Write technical documentation",
]

_MODEL_NAME = "all-MiniLM-L6-v2"


class TopicClassifier:
    """
    Zero-shot embedding classifier for on-domain / off-topic detection.

    Parameters
    ----------
    domain_description : str
        Plain-English description of valid usage, e.g.
        "Retail banking AI assistant for account balances and transfers".
        Appended to on-topic anchors at load time.
    threshold : float
        Off-topic cosine similarity threshold (default 0.72).
        Higher = stricter (more prompts flagged as off-topic).
    """

    def __init__(
        self,
        domain_description: str,
        threshold: float = 0.72,
    ) -> None:
        self._threshold = threshold
        self._model     = None
        self._np        = None
        self._off_emb   = None
        self._on_emb    = None
        self._ready     = False
        self._load(domain_description)

    def is_off_topic(self, text: str) -> bool:
        """
        Returns True when the prompt is likely off-topic for the configured domain.
        Returns False (fail-open) if the model failed to load.
        """
        if not self._ready:
            return False

        emb = self._model.encode([text], normalize_embeddings=True)   # (1, D)
        off_score = float(self._np.max(emb @ self._off_emb.T))
        on_score  = float(self._np.max(emb @ self._on_emb.T))

        logger.debug(
            "TopicClassifier: off=%.3f on=%.3f threshold=%.2f text='%.60s'",
            off_score, on_score, self._threshold, text,
        )

        # Flag only when clearly off-topic AND clearly not on-topic
        return off_score > self._threshold and off_score > on_score

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self, domain_description: str) -> None:
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer

            self._np    = np
            self._model = SentenceTransformer(_MODEL_NAME)

            on_anchors  = _ON_TOPIC_BASE + ([domain_description] if domain_description else [])
            self._off_emb = self._model.encode(_OFF_TOPIC,   normalize_embeddings=True)
            self._on_emb  = self._model.encode(on_anchors,   normalize_embeddings=True)
            self._ready   = True
            logger.info("PrivacyProxy: TopicClassifier ready (%s)", _MODEL_NAME)

        except ImportError:
            logger.warning(
                "PrivacyProxy: sentence-transformers not installed — "
                "Stage 2 topic classifier disabled. "
                "Install with: pip install sentence-transformers"
            )
        except Exception as exc:
            logger.warning("PrivacyProxy: TopicClassifier failed to load: %s", exc)
