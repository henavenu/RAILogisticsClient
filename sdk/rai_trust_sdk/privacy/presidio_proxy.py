"""
PresidioPrivacyProxy — NLP-based PII detection using Microsoft Presidio.

Why Presidio?
─────────────
The regex-based PrivacyProxy catches structured PII that fits a fixed pattern:
SSNs in NNN-NN-NNNN format, 16-digit card numbers, email addresses, etc.
It does NOT catch:

  • Person names in prose         ("Wire funds to Eleanor Hartfield-Morse")
  • Organisation names            ("Goldman Sachs client account")
  • Free-form SSN variations      ("five-two-three dash forty-five")
  • US ITIN / passport numbers
  • IBANs                         (international account numbers)
  • IP addresses

Presidio uses spaCy Named Entity Recognition (NER) plus its own rule-based
recognizers to detect all of the above in natural language, not just in
structured patterns.

Combined strategy (three passes run sequentially):
─────────────────────────────────────────────────
  Pass 1 — Regex (all structured PII)
            Calls PrivacyProxy._anonymize_pii() which covers credentials
            (api_key, password, private_key) and all structured patterns
            (routing/account numbers, SSN, email, phone, credit card).
            Zero ML dependencies.  Runs first to consume structured numbers
            before Presidio's phone/SSN recognizers might mis-label them.

  Pass 2 — Presidio NLP (unstructured + named-entity PII)
            Catches what regex misses: person names, organisation names,
            IBANs, IP addresses, US passports, driver licences, etc.

  Pass 3 — Entity tokenisation (ProxyConfig.project_names / client_names)
            Case-insensitive string replacement for configured entity lists.

Requirements
────────────
    pip install rai-trust-sdk[presidio]
    python -m spacy download en_core_web_lg   # recommended
    # or: python -m spacy download en_core_web_sm  # smaller, less accurate

Falls back gracefully if presidio-analyzer is not installed — Pass 1 and
Pass 3 still run, Pass 2 is skipped with a single warning.

Usage
─────
    from rai_trust_sdk.privacy import create_proxy, ProxyConfig

    proxy = create_proxy(
        provider="presidio",
        config=ProxyConfig(
            project_names=["Project Titan"],
            client_names=["Acme Corp"],
        ),
    )

    result = proxy.process(
        "Wire $50,000 to Eleanor Hartfield-Morse, SSN 523-45-6789, "
        "IBAN GB33BUKB20201555555555, re: Project Titan"
    )
    # result.anonymized →
    #   "Wire $50,000 to [PERSON], SSN [SSN], IBAN [IBAN], re: [PROJECT_1]"
    # result.detections → ["ssn", "PERSON", "IBAN_CODE", "Project Titan"]
    # result.blocked    → True  (PII was found)
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from .proxy import PrivacyProxy, ProxyConfig, ProxyResult

logger = logging.getLogger(__name__)


# ── Entity type → anonymisation token mapping ─────────────────────────────────
#
# Presidio returns entity_type labels (e.g. "PERSON", "US_SSN").  We map them
# to our bracketed token format so the Policy Engine sees consistent tokens
# regardless of which proxy provider is active.
#
# Types not listed here get a generic [<TYPE>] token via the DEFAULT operator.

_ENTITY_TOKEN_MAP: dict[str, str] = {
    "PERSON":            "[PERSON]",
    "EMAIL_ADDRESS":     "[EMAIL]",
    "PHONE_NUMBER":      "[PHONE]",
    "US_SSN":            "[SSN]",
    "US_ITIN":           "[SSN]",            # same token for consistency
    "CREDIT_CARD":       "[CREDIT_CARD]",
    "US_BANK_NUMBER":    "[BANK_ACCOUNT]",
    "IBAN_CODE":         "[IBAN]",
    "IP_ADDRESS":        "[IP_ADDRESS]",
    "URL":               "[URL]",
    "LOCATION":          "[LOCATION]",
    "NRP":               "[NRP]",             # Nationality / Religion / Political
    "MEDICAL_LICENSE":   "[MEDICAL_LICENSE]",
    "DATE_TIME":         "[DATE]",
    "US_PASSPORT":       "[PASSPORT]",
    "US_DRIVER_LICENSE": "[DL]",
    "CRYPTO":            "[CRYPTO]",          # Bitcoin / Ethereum wallet addresses
    "IN_PAN":            "[FINANCIAL_ID]",    # India PAN card
}

# Entity type list passed to AnalyzerEngine.analyze().
# Presidio resolves overlapping spans by confidence + length — ordering here is
# documentation order only.
_ENTITIES = list(_ENTITY_TOKEN_MAP.keys())


class PresidioPrivacyProxy:
    """
    NLP-based PII detection and anonymization using Microsoft Presidio.

    Implements the same ``.process(text) → ProxyResult`` interface as
    PrivacyProxy so it can be passed directly to instrument() as
    privacy_proxy=.

    Thread safety: AnalyzerEngine and AnonymizerEngine are thread-safe once
    loaded.  A single instance can be shared safely across threads.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config     = config
        self._entities   = self._build_entity_list()
        self._analyzer:  Optional[Any] = None
        self._anonymizer: Optional[Any] = None
        self._ready      = False
        self._load()

    # ── Public ────────────────────────────────────────────────────────────────

    def process(self, text: str) -> ProxyResult:
        """
        Detect and anonymize PII in *text*.

        Pass 1 — PrivacyProxy regex     (all structured PII + credentials)
        Pass 2 — Presidio NLP           (person names, orgs, IBANs, etc.)
        Pass 3 — Entity tokenization    (project/client names from ProxyConfig)

        Returns ProxyResult.  ``anonymized`` always contains the masked text;
        ``blocked`` is True when any PII was detected.

        If Presidio is not installed, Pass 2 is skipped.
        """
        anonymized = text
        detections: List[str] = []

        # Pass 1 — Structured PII + credentials (pure regex, zero ML)
        # PrivacyProxy._anonymize_pii() runs all _PII_PATTERNS in order:
        # routing_number, bank_account, credit_card, ssn, email, phone,
        # api_key, password, private_key.
        anonymized, regex_hits = PrivacyProxy._anonymize_pii(anonymized)
        detections.extend(regex_hits)

        # Pass 2 — Presidio NLP: unstructured + named-entity PII
        # Runs on already-regex-anonymized text so structured numbers that
        # were already replaced (e.g. "[SSN]") are not re-evaluated.
        if self._ready:
            anonymized, presidio_hits = self._presidio_anonymize(anonymized)
            detections.extend(presidio_hits)
        else:
            logger.debug(
                "PresidioPrivacyProxy: NLP engine not available — "
                "Pass 2 skipped (install rai-trust-sdk[presidio] to enable)"
            )

        # Pass 3 — Named entities from ProxyConfig (project/client names)
        anonymized, entity_hits = self._tokenize_entities(anonymized)
        detections.extend(entity_hits)

        if detections:
            logger.debug("PresidioPrivacyProxy obfuscated: %s", detections)

        return ProxyResult(
            blocked=bool(detections),
            reason=(
                f"Obfuscated via Presidio NLP: {', '.join(detections)}"
                if detections else ""
            ),
            detections=detections,
            anonymized=anonymized,
        )

    # ── Internal — loader ─────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load Presidio AnalyzerEngine and AnonymizerEngine at startup."""
        try:
            from presidio_analyzer   import AnalyzerEngine   # noqa: F401
            from presidio_anonymizer import AnonymizerEngine  # noqa: F401

            self._analyzer   = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
            self._ready      = True
            logger.info(
                "PresidioPrivacyProxy: NLP engine ready — %d entity types",
                len(_ENTITIES),
            )
        except ImportError:
            logger.warning(
                "PresidioPrivacyProxy: presidio-analyzer not installed — "
                "NLP PII detection disabled.  Credential regex (Pass 1) and "
                "entity name tokenization (Pass 3) will still run.  "
                "To enable NLP:\n"
                "  pip install rai-trust-sdk[presidio]\n"
                "  python -m spacy download en_core_web_lg"
            )
        except Exception as exc:
            logger.warning(
                "PresidioPrivacyProxy: engine failed to load (%s) — "
                "Check spaCy model installation: "
                "python -m spacy download en_core_web_lg",
                exc,
            )

    # ── Internal — Pass 2 ─────────────────────────────────────────────────────

    def _presidio_anonymize(self, text: str) -> Tuple[str, List[str]]:
        """
        Run Presidio NLP analysis + anonymization on already-regex-masked text.

        Returns (anonymized_text, list_of_entity_type_labels).

        Presidio resolves overlapping spans by confidence then span length;
        the highest-confidence / longest span wins.  We build a per-type
        operator map from _ENTITY_TOKEN_MAP so each entity gets its own token.
        """
        from presidio_anonymizer.entities import OperatorConfig  # noqa: F401

        analyzer_results = self._analyzer.analyze(
            text=text,
            language="en",
            entities=_ENTITIES,
        )

        if not analyzer_results:
            return text, []

        # Build operator map: entity_type → replace with named token
        operators: dict[str, Any] = {
            entity_type: OperatorConfig("replace", {"new_value": token})
            for entity_type, token in _ENTITY_TOKEN_MAP.items()
        }
        # Catch any entity types Presidio returns that aren't in our map
        operators["DEFAULT"] = OperatorConfig("replace", {"new_value": "[REDACTED]"})

        anon_result = self._anonymizer.anonymize(
            text=text,
            analyzer_results=analyzer_results,
            operators=operators,
        )

        # Unique entity type labels detected
        hits = sorted({r.entity_type for r in analyzer_results})
        return anon_result.text, hits

    # ── Internal — Pass 3 ─────────────────────────────────────────────────────

    def _build_entity_list(self) -> List[Tuple[str, str]]:
        """
        Build (name, replacement_token) pairs from ProxyConfig, sorted
        longest-first to avoid partial matches ("Acme Corp" before "Acme").
        """
        entries: List[Tuple[str, str]] = []
        for i, name in enumerate(self._config.project_names, 1):
            entries.append((name, f"[PROJECT_{i}]"))
        for i, name in enumerate(self._config.client_names, 1):
            entries.append((name, f"[CLIENT_{i}]"))
        return sorted(entries, key=lambda x: len(x[0]), reverse=True)

    def _tokenize_entities(self, text: str) -> Tuple[str, List[str]]:
        """
        Case-insensitive replacement of configured entity names with tokens.
        Returns (anonymized_text, list_of_matched_entity_names).
        """
        anonymized = text
        hits: List[str] = []
        for name, token in self._entities:
            if re.search(re.escape(name), anonymized, re.IGNORECASE):
                anonymized = re.sub(
                    re.escape(name), token, anonymized, flags=re.IGNORECASE
                )
                hits.append(name)
        return anonymized, hits
