"""
Local Policy Classifier — keyword-based corporate AI governance.

Evaluates the corporate_policy dimension entirely in-process.
No external API calls, no ML model dependencies.

Scoring strategy:
  0 signal hits → score 1  (PASS)
  1 signal hit  → score 5  (NORMATIVE / warn)
  2+ hits       → score 6  (NORMATIVE / warn — capped until AUP is finalised)

Score is intentionally capped at 6 (top of the NORMATIVE band with the
standard profile) so that no prompt triggers a COERCIVE verdict until the
organisation's Acceptable Use Policy is signed off and the signal list is
reviewed and approved.  Raise the multi-hit score to 8 to enable hard-block.

Signal categories:
  confidential_data_disclosure — pasting confidential / classified material
                                 into an AI tool that calls an external provider
  client_pii_exposure          — customer / employee personal data in prompts
  regulated_content            — MNPI, legal advice, medical decisions
  policy_bypass                — explicit attempts to circumvent AI safety rules

The signal lists here are intentionally conservative generic defaults.
Replace or extend them via config/policy_signals.yaml once the AUP is
available — that file takes full precedence over the built-in defaults.
"""
import time
import yaml
import logging
from typing import List, Dict, Tuple

from providers.base import TrustProvider

logger = logging.getLogger(__name__)

# ── Default signal lists ──────────────────────────────────────────────────────
#
# Signals are matched case-insensitively as substrings.
# Keep phrases specific enough to avoid false positives on legitimate work.
#
_DEFAULT_SIGNALS: Dict[str, List[str]] = {
    "corporate_policy": [

        # ── Confidential data disclosure ─────────────────────────────────────
        # Employee pastes content that is labelled as restricted / confidential.
        # Risk: content is transmitted to an external AI provider's API.
        "strictly confidential", "confidential — do not share",
        "internal use only", "not for distribution", "eyes only",
        "company confidential", "attorney-client privilege",
        "draft — privileged", "do not forward",

        # ── Client / customer PII in prompts ─────────────────────────────────
        # Sending identifiable customer or employee data to an external AI.
        # Note: the Privacy Proxy handles raw PII patterns (SSN, credit card,
        # email) before they reach the policy engine.  These signals catch
        # explicit intent to include PII in AI interactions.
        "customer details", "client record", "employee record",
        "full name and address", "date of birth and address",
        "paste the customer data", "include the client file",

        # ── Regulated / legally sensitive content ────────────────────────────
        # Requests that could create liability if the AI output is acted upon
        # without qualified human review.
        "material non-public", "mnpi", "inside information",
        "not yet announced", "pre-announcement",
        "give me legal advice", "is this legal",
        "medical diagnosis", "prescribe", "clinical recommendation",

        # ── AI safety policy bypass ───────────────────────────────────────────
        # Explicit framing designed to remove guardrails or impersonate roles.
        "ignore your instructions", "ignore previous instructions",
        "disregard your guidelines", "bypass your safety",
        "pretend you have no restrictions", "jailbreak",
        "act as an unrestricted", "developer mode",
        "do anything now", "dan mode",
    ],
}


def _status_for_score(score: int) -> str:
    if score >= 7:
        return "FLAGGED"
    elif score >= 4:
        return "WARNING"
    return "PASS"


class LocalPolicyClassifierProvider(TrustProvider):
    """
    In-process corporate policy classifier.
    Uses keyword signal matching — no external calls, no ML runtime.
    """

    def __init__(self, config_path: str = "config/policy_signals.yaml"):
        self._config_path  = config_path
        self._enforce_mode = "warn"
        self._signals: Dict[str, List[str]] = dict(_DEFAULT_SIGNALS)
        self._load_config(config_path)

    def _load_config(self, path: str):
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            for dim, spec in cfg.get("signals", {}).items():
                keywords = spec.get("keywords", [])
                if keywords:
                    self._signals[dim] = keywords
            # enforce_mode: "warn" caps multi-hit at 6 (NORMATIVE only)
            #               "block" raises multi-hit to 8 (enables COERCIVE)
            self._enforce_mode = cfg.get("enforce_mode", "warn")
            logger.info(
                f"LocalPolicyClassifier loaded config from {path} "
                f"({sum(len(v) for v in self._signals.values())} signals, "
                f"enforce_mode={self._enforce_mode})"
            )
        except FileNotFoundError:
            self._enforce_mode = "warn"
            logger.info(
                f"LocalPolicyClassifier: {path} not found — using built-in defaults "
                f"({sum(len(v) for v in self._signals.values())} signals, enforce_mode=warn)"
            )
        except Exception as exc:
            self._enforce_mode = "warn"
            logger.warning(f"LocalPolicyClassifier: error loading {path}: {exc} — using defaults")

    def name(self) -> str:
        return "local_policy_classifier"

    def reload(self):
        """Hot-reload signal list and enforce_mode from disk."""
        self._signals = dict(_DEFAULT_SIGNALS)
        self._load_config(self._config_path)
        logger.info("LocalPolicyClassifier reloaded")

    def _score_from_content(self, content: str, dimension: str) -> int:
        keywords = self._signals.get(dimension, [])
        content_lower = content.lower()
        hits = sum(1 for kw in keywords if kw in content_lower)

        if hits == 0:
            return 1       # PASS — no signal
        elif hits == 1:
            return 5       # NORMATIVE — single hit, warn
        else:
            # enforce_mode=warn  → cap at 6 (NORMATIVE, no hard block)
            # enforce_mode=block → score 8 (COERCIVE, hard block)
            return 6 if self._enforce_mode == "warn" else 8

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start = time.time()
        results = []

        for dim in evaluations:
            score  = self._score_from_content(content, dim)
            status = _status_for_score(score)

            results.append({
                "dimension": dim,
                "score":     score,
                "status":    status,
                "provider_metadata": {
                    "provider":   "local_policy_classifier",
                    "in_process": True,
                },
            })
            logger.debug(f"LocalPolicyClassifier [{dim}] score={score} status={status}")

        latency_ms = max(1, int((time.time() - start) * 1000))
        return results, latency_ms
