"""
PrivacyProxy — two-stage in-process content filter.

Stage 1  Pattern matching + entity tokenization (pure Python, no network)
         Detects PII, credentials, and prohibited named entities.
         PII is anonymised in-place (replaced with tokens such as
         [ROUTING_NUMBER], [EMAIL], etc.) so the downstream Policy Engine
         evaluates the sanitised text — never the raw sensitive values.

Stage 2  Local embedding classifier (sentence-transformers, no network)
         Detects off-topic / irrelevant requests.
         Falls back gracefully if sentence-transformers not installed.

Usage::

    from rai_trust_sdk.privacy import PrivacyProxy, ProxyConfig

    proxy = PrivacyProxy(ProxyConfig(
        project_names=["Project Titan", "Project Aurora"],
        client_names=["Acme Corp", "Goldman Sachs"],
        domain_description="Retail banking AI assistant for account and transfer queries",
    ))

    result = proxy.process("Transfer $200 to routing 021000021 account 1234567890")
    # result.anonymized == "Transfer $200 to routing [ROUTING_NUMBER] account [BANK_ACCOUNT]"
    # result.detections == ["routing_number", "bank_account"]
    # result.blocked    == True  (raw PII detected; PE still receives anonymised text)

    result2 = proxy.process("Help me with Project Titan — my email is alice@example.com")
    # result2.anonymized == "Help me with [PROJECT_1] — my email is [EMAIL]"
    # result2.detections == ["email", "Project Titan"]
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── PII / credential patterns ────────────────────────────────────────────────
# Each entry: (label, compiled_regex, replacement_callable)
#
# replacement_callable receives the re.Match object and returns the
# substitution string.  Use ``lambda m: '[TOKEN]'`` for full-match replacement,
# or ``lambda m: m.group(1) + ' [TOKEN]'`` to preserve a context keyword
# (e.g. keep "routing" but replace the digits).
_PII_PATTERNS: List[Tuple[str, re.Pattern, Callable]] = [
    # ── Financial identifiers (GLBA NPI / NACHA-protected) ───────────────────
    # Routing numbers and bank account numbers are NOT PCI DSS (which covers
    # card PANs only) but they ARE "nonpublic personal information" under GLBA
    # and protected data under NACHA operating rules.  They must not appear in:
    #   • requests sent to external Guardrail Service providers
    #   • AI governance audit trails (which have weaker access controls than
    #     the core banking transaction record)
    # They are tokenised here for the PE evaluation copy / audit trail.
    #
    # IMPORTANT — shadow-copy pattern required:
    #   These tokens must only replace content in the *evaluation copy* passed
    #   to the Policy Engine.  The LangGraph execution state must retain the
    #   original values so tool calls (domestic_transfer, etc.) receive real
    #   routing/account numbers.  See router_node() in graph.py — it uses
    #   proxy_result.anonymized for the PE call while leaving graph state /
    #   user_text untouched so downstream tool calls receive real values.
    #
    # ORDERING — financial patterns MUST run before phone/credit_card:
    #   A 10-digit US account number is indistinguishable from a phone number
    #   by digit count alone.  Running the keyword-anchored bank_account and
    #   routing_number patterns first consumes the digits so the generic phone
    #   pattern cannot claim them.

    # "routing 021000021"              → "routing [ROUTING_NUMBER]"
    # "routing number 021000021"       → "routing number [ROUTING_NUMBER]"
    # "aba 026009593"                  → "aba [ROUTING_NUMBER]"
    # "transit number 021000021"       → "transit number [ROUTING_NUMBER]"
    #
    # Pattern structure (simpler and unambiguous):
    #   keyword + optional(space + "number/num/no") + space + 9 digits
    # The separator space is OUTSIDE the optional (number) group so the regex
    # engine never backtracks into ambiguous territory when "number" is absent.
    ("routing_number",
     re.compile(
         r'(?i)\b((?:routing|aba|transit)(?:\s+(?:number|num|no\.?))?)\s+\d{9}\b'
     ),
     lambda m: m.group(1) + ' [ROUTING_NUMBER]'),

    # "account 1234567890"             → "account [BANK_ACCOUNT]"
    # "account number 1234567890"      → "account number [BANK_ACCOUNT]"
    # "acct 1234567890"                → "acct [BANK_ACCOUNT]"
    ("bank_account",
     re.compile(
         r'(?i)\b((?:account|acct?)(?:\s+(?:number|num|no\.?))?)\s+\d{6,17}\b'
     ),
     lambda m: m.group(1) + ' [BANK_ACCOUNT]'),

    # ── Standard PII / credentials ────────────────────────────────────────────
    # credit_card before phone: 16-digit card numbers must not be parsed as
    # two overlapping 10-digit phone numbers.
    ("credit_card",
     re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),
     lambda m: '[CREDIT_CARD]'),

    ("ssn",
     re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
     lambda m: '[SSN]'),

    ("email",
     re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
     lambda m: '[EMAIL]'),

    # phone last among numeric patterns — catches remaining 10-digit sequences
    # that were not consumed by the keyword-anchored patterns above.
    ("phone",
     re.compile(r'\b(\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b'),
     lambda m: '[PHONE]'),

    ("api_key",
     re.compile(r'(?i)(sk-|api[_-]?key\s*[=:]\s*|bearer\s+)[A-Za-z0-9_\-\.]{20,}'),
     lambda m: '[API_KEY]'),

    # Keep keyword, replace value: "password=secret" → "password=[PASSWORD]"
    ("password",
     re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*\S{6,}'),
     lambda m: m.group(1) + '=[PASSWORD]'),

    ("private_key",
     re.compile(r'-----BEGIN\s+(?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
     lambda m: '[PRIVATE_KEY]'),
]


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class ProxyConfig:
    """
    Configuration for PrivacyProxy.

    project_names       Internal project / initiative names to block.
    client_names        Client or partner names to block.
    domain_description  Plain-English description of valid usage for the app.
                        Used by the Stage 2 topic classifier.
    off_topic_threshold Cosine similarity above which a prompt is considered
                        off-topic (0.0–1.0, default 0.72).
    enable_classifier   Set False to skip Stage 2 (e.g. sentence-transformers
                        not installed, or latency budget too tight).
    """
    project_names:       List[str] = field(default_factory=list)
    client_names:        List[str] = field(default_factory=list)
    domain_description:  str       = ""
    off_topic_threshold: float     = 0.72
    enable_classifier:   bool      = True


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class ProxyResult:
    blocked:        bool
    reason:         str        = ""
    detections:     List[str]  = field(default_factory=list)
    anonymized:     str        = ""   # original text with PII/entities tokenized


# ── Proxy ─────────────────────────────────────────────────────────────────────

class PrivacyProxy:
    """
    In-process two-stage content filter.
    Instantiate once at startup and reuse across requests.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config     = config
        self._entities   = self._build_entity_list()
        self._classifier: Optional[object] = None   # lazy-loaded

    # ── Public ────────────────────────────────────────────────────────────────

    def process(self, text: str) -> ProxyResult:
        """
        Run text through both stages.

        Stage 1a anonymises detected PII in-place (replaces with tokens).
        Stage 1b tokenises prohibited entity names.
        Stage 2  classifies topic relevance (requires sentence-transformers).

        Returns ProxyResult immediately once any detection occurs.
        When detections is non-empty, ``anonymized`` always differs from the
        original text — the caller (or GovernedGraph middleware) can substitute
        it for the original without sending raw PII to external systems.
        """
        # Stage 1a — PII patterns: detect and anonymise in-place
        anonymized, pii = self._anonymize_pii(text)

        # Stage 1b — prohibited entity tokenization (on already-anonymised text)
        anonymized, entity_hits = self._tokenize_entities(anonymized)

        if pii or entity_hits:
            all_hits = pii + entity_hits
            return ProxyResult(
                # blocked=True when raw PII was found (informational — the caller
                # may choose to reject; GovernedGraph middleware always forwards
                # anonymised text to the PE for enforcement).
                blocked=bool(pii),
                reason=f"Content anonymised: {', '.join(all_hits)}",
                detections=all_hits,
                anonymized=anonymized,
            )

        # Stage 2 — topic relevance classifier (local, no network)
        if self._config.enable_classifier and self._config.domain_description:
            try:
                clf = self._get_classifier()
                if clf.is_off_topic(anonymized):
                    return ProxyResult(
                        blocked=True,
                        reason="Request is not relevant to permitted work activities.",
                        detections=["off_topic"],
                        anonymized=anonymized,
                    )
            except Exception as exc:
                logger.warning("PrivacyProxy: classifier error (fail-open): %s", exc)

        return ProxyResult(blocked=False, anonymized=anonymized)

    # ── Internal — Stage 1a ───────────────────────────────────────────────────

    @staticmethod
    def _anonymize_pii(text: str) -> Tuple[str, List[str]]:
        """
        Detect PII patterns and replace them with anonymisation tokens.

        Returns ``(anonymized_text, list_of_hit_labels)``.
        Runs all patterns so multiple PII types in the same text are all replaced.
        """
        anonymized = text
        hits: List[str] = []
        for label, pattern, replacer in _PII_PATTERNS:
            if pattern.search(anonymized):
                anonymized = pattern.sub(replacer, anonymized)
                hits.append(label)
        return anonymized, hits

    # ── Internal — Stage 1b ───────────────────────────────────────────────────

    def _build_entity_list(self) -> List[Tuple[str, str]]:
        """Build (name, replacement_token) sorted longest-first."""
        entries: List[Tuple[str, str]] = []
        for i, name in enumerate(self._config.project_names, 1):
            entries.append((name, f"[PROJECT_{i}]"))
        for i, name in enumerate(self._config.client_names, 1):
            entries.append((name, f"[CLIENT_{i}]"))
        return sorted(entries, key=lambda x: len(x[0]), reverse=True)

    def _tokenize_entities(self, text: str) -> Tuple[str, List[str]]:
        """Replace prohibited entity names with tokens. Returns (anonymized, hits)."""
        anonymized = text
        hits: List[str] = []
        for name, token in self._entities:
            if re.search(re.escape(name), anonymized, re.IGNORECASE):
                anonymized = re.sub(re.escape(name), token, anonymized, flags=re.IGNORECASE)
                hits.append(name)
        return anonymized, hits

    # ── Internal — Stage 2 ────────────────────────────────────────────────────

    def _get_classifier(self) -> object:
        if self._classifier is None:
            from .classifier import TopicClassifier
            self._classifier = TopicClassifier(
                domain_description=self._config.domain_description,
                threshold=self._config.off_topic_threshold,
            )
        return self._classifier
