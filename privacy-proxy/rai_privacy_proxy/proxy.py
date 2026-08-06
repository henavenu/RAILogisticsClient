"""
PrivacyProxy — Stage 1 in-process one-way obfuscation.

Replaces sensitive values with placeholder tokens before any content
reaches an LLM or external service.  Pure Python, no network calls,
no heavyweight ML dependencies.

Design principle
────────────────
The proxy is a one-way obfuscation layer ONLY.  It never blocks a request.
``result.blocked`` is always ``False``.

The Policy Engine receives the anonymised text and makes all enforcement
decisions.  Keeping blocking out of the proxy preserves the audit trail —
every request reaches the PE, so every evaluation is recorded.

Anonymisation tracks
────────────────────
  Credentials  — api_key, password, private_key
      Replaced with ``[API_KEY]``, ``[PASSWORD]``, ``[PRIVATE_KEY]`` so
      the raw secret never leaves the process.

  PII          — iban, swift_bic, phone, email, ssn, credit_card
      Replaced with ``[IBAN]``, ``[SWIFT]``, ``[PHONE]``, ``[EMAIL]``, ``[SSN]``, ``[CARD]``.
      In a banking context these appear in ordinary messages
      (account numbers, routing numbers, customer contact details).

  Entities     — project_names, client_names (from ProxyConfig)
      Replaced with ``[PROJECT_N]``, ``[CLIENT_N]`` tokens.

Usage::

    from rai_privacy_proxy import PrivacyProxy, ProxyConfig

    proxy = PrivacyProxy(ProxyConfig(
        project_names=["Project Titan", "Project Aurora"],
        client_names=["Acme Corp"],
    ))

    result = proxy.process("Wire $500 to account 1234567890 re: Project Titan")
    # result.blocked    → False  (always)
    # result.anonymized → "Wire $500 to account [ACCOUNT] re: [PROJECT_1]"
    # result.detections → ["account_num", "Project Titan"]
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Credential patterns ───────────────────────────────────────────────────────
# Raw secrets are replaced with named tokens so the PE evaluates intent,
# not the secret value itself.
# Each entry: (label, replacement_token, compiled_regex)
_CREDENTIAL_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    ("api_key",     "[API_KEY]",     re.compile(r'(?i)(sk-|api[_-]?key\s*[=:]\s*|bearer\s+)[A-Za-z0-9_\-\.]{20,}')),
    ("password",    "[PASSWORD]",    re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*\S{6,}')),
    ("private_key", "[PRIVATE_KEY]", re.compile(r'-----BEGIN\s+(?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
]

# ── PII patterns ──────────────────────────────────────────────────────────────
# Personal identifiers that appear routinely in banking messages.
# Replaced with descriptive tokens; context and intent are preserved.
# Each entry: (label, replacement_string, compiled_regex)
#
# replacement_string may be a plain token ("[ PHONE]") or a backreference
# string (r"\1[ACCOUNT]") that preserves a keyword prefix captured in group 1.
# Python's re.sub() handles both forms transparently.
#
# Ordering matters:
#   1. Account / routing numbers first (keyword-anchored, most specific).
#   2. IBAN before credit card — a spaced IBAN contains 4-4-4-4 digit groups
#      that the credit-card pattern would otherwise consume first.
#   3. SWIFT/BIC before credit card (no numeric overlap, but keeps financial
#      identifiers grouped together).
#   4. Credit card before phone (16-digit card numbers trump 10-digit phone match).
#   5. Phone last among numeric patterns (catches remaining 10-digit sequences).
_PII_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    # Bank account numbers: digit sequence (6–17 digits) following account keywords.
    # Group 1 captures the keyword + optional label so it is preserved in output,
    # e.g. "account 1234567890" → "account [ACCOUNT]".
    (
        "account_num",
        r"\g<1>[ACCOUNT]",
        re.compile(
            r"(?i)((?:account|acct?)[\s\-#]*(?:number|num|no\.?)?\s*)(\d{6,17})"
        ),
    ),
    # Routing / ABA / transit numbers: exactly 9 digits following routing keywords.
    # Routing numbers identify a bank branch, not the individual, but in a transfer
    # request the combination with an account number is sensitive.
    (
        "routing_num",
        r"\g<1>[ROUTING]",
        re.compile(
            r"(?i)((?:routing|aba|transit)[\s\-#]*(?:number|num|no\.?)?\s*)(\d{9})"
        ),
    ),
    # IBAN: 2-letter country code + 2 check digits + BBAN (11–30 alphanumeric chars).
    # Matches both spaced (DE89 3704 0044 0532 0130 00) and unspaced
    # (DE89370400440532013000) formats.  Must come BEFORE credit_card because
    # a spaced IBAN contains 4-4-4-4 digit groups that would otherwise match
    # the credit-card pattern.
    (
        "iban",
        "[IBAN]",
        re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),
    ),
    # SWIFT / BIC codes: 8 or 11 uppercase alphanumeric characters.
    # Format: 4-letter bank code + 2-letter country code + 2-char location code
    # + optional 3-char branch code.  E.g. COBADEFFXXX, COBADEFF, MELIIRTHXXX.
    #
    # Context-anchored: requires "SWIFT" or "BIC" label to precede the code.
    # A standalone pattern ([A-Z]{8} or [A-Z]{11}) would match any uppercase
    # English word (TRANSFER, APPROVED, etc.), producing too many false positives.
    # LLM banking confirmations always write the code with a label, so the
    # context requirement does not reduce recall in practice.
    # Group 1 captures the label prefix so it is preserved in the output;
    # only the code itself is replaced with [SWIFT].
    (
        "swift_bic",
        r"\g<1>[SWIFT]",
        re.compile(
            r"(?i)((?:swift|bic)[\s:/]*)([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b"
        ),
    ),
    # Credit / debit card numbers (16 digits, grouped 4-4-4-4) — before phone
    # pattern to prevent partial overlap on formatted long numbers.
    ("credit_card", "[CARD]",   re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")),
    # Social security numbers (NNN-NN-NNNN format).
    ("ssn",         "[SSN]",    re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Email addresses.
    ("email",       "[EMAIL]",  re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # Standalone phone numbers (10-digit, with or without separators).
    # Applied after account/card patterns so 10-digit account numbers without
    # keywords are still caught here as a safety net.
    ("phone",       "[PHONE]",  re.compile(r"\b(\+\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")),
]


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class ProxyConfig:
    """
    Configuration for PrivacyProxy.

    project_names   Internal project / initiative names to obfuscate.
    client_names    Client or partner names to obfuscate.
    """
    project_names: List[str] = field(default_factory=list)
    client_names:  List[str] = field(default_factory=list)


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class ProxyResult:
    blocked:    bool          # always False — proxy never blocks
    reason:     str       = ""
    detections: List[str] = field(default_factory=list)
    anonymized: str       = ""   # original text with sensitive values tokenized


# ── Proxy ─────────────────────────────────────────────────────────────────────

class PrivacyProxy:
    """
    In-process Stage 1 one-way obfuscation filter.
    Instantiate once at startup and reuse across requests.

    The proxy never blocks.  It replaces sensitive values with placeholder
    tokens and returns the sanitised text for Policy Engine evaluation.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config   = config
        self._entities = self._build_entity_list()

    # ── Public ────────────────────────────────────────────────────────────────

    def process(self, text: str) -> ProxyResult:
        """
        Obfuscate sensitive values in *text*.

        All three passes run unconditionally; the result is always
        ``blocked=False`` with ``anonymized`` carrying the masked text.

        Pass 1 — credential anonymisation  (api_key, password, private_key)
        Pass 2 — PII anonymisation         (phone, email, ssn, credit_card)
        Pass 3 — entity tokenisation       (project_names, client_names)
        """
        anonymized = text
        detections: List[str] = []

        # Pass 1 — credentials
        anonymized, cred_hits = _apply_patterns(anonymized, _CREDENTIAL_PATTERNS)
        detections.extend(cred_hits)

        # Pass 2 — PII
        anonymized, pii_hits = _apply_patterns(anonymized, _PII_PATTERNS)
        detections.extend(pii_hits)

        # Pass 3 — prohibited entities
        anonymized, entity_hits = self._tokenize_entities(anonymized)
        detections.extend(entity_hits)

        if detections:
            logger.debug("PrivacyProxy obfuscated: %s", detections)

        return ProxyResult(
            blocked=False,
            reason=f"Obfuscated: {', '.join(detections)}" if detections else "",
            detections=detections,
            anonymized=anonymized,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_entity_list(self) -> List[Tuple[str, str]]:
        """Build (name, replacement_token) list sorted longest-first to avoid partial matches."""
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


# ── Module-level helpers ──────────────────────────────────────────────────────

def _apply_patterns(
    text: str,
    patterns: List[Tuple[str, str, re.Pattern]],
) -> Tuple[str, List[str]]:
    """
    Apply a list of (label, token, regex) patterns to *text*.
    Each match is replaced with the corresponding token.
    Returns (anonymized_text, list_of_detected_labels).
    """
    anonymized = text
    hits: List[str] = []
    for label, token, pattern in patterns:
        if pattern.search(anonymized):
            anonymized = pattern.sub(token, anonymized)
            hits.append(label)
    return anonymized, hits
