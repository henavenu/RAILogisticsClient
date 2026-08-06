"""
Transfer Security Provider — in-process prompt injection and jailbreak detection.

Covers two evaluation dimensions:
  prompt_injection — role-boundary violation, delimiter smuggling, template
                     injection, impersonation of system/developer identity
  jailbreak        — constraint removal, authority impersonation, hypothetical
                     bypass, persistence-command escalation

Three detection layers run entirely in-process with zero external API calls:

  Layer 1 — Regex signal groups (compiled once at module load)
    STRONG groups: high-confidence; one group hit contributes a 2× weight vs weak.
    WEAK groups:   moderate-confidence; two group hits equal one strong hit.

  Layer 2 — Structural/linguistic heuristics (secondary signal)
    Blank-line injection, excessive bracket density, unicode anomalies (zero-
    width chars, RTL overrides).  Two or more heuristic types fire → +1 weak
    equivalent.

  Layer 3 — Transfer-context amplifier
    When transfer-redirect signals (memo injection, account/beneficiary
    substitution, amount override) co-occur with injection or jailbreak
    patterns, the score is raised by +2 (capped at 9).  This makes the
    provider specifically effective for the finapp transfers use-case where
    tool memo / beneficiary fields are a natural injection vector.

Score → Verdict mapping (standard 1-9 scale):
  1       PASS      (PERMISSIVE)  — no signal
  3       PASS      (PERMISSIVE)  — single weak signal, below threshold
  5       WARNING   (NORMATIVE)   — moderate confidence
  7       FLAGGED   (COERCIVE)    — high confidence
  9       FLAGGED   (COERCIVE)    — maximum / certain

Provider attributes:
  in_process : True  — no network, no credentials, no warm-up latency
  fail_mode  : fail_open — score=1/PASS on unexpected exception
  mock_exempt: True  — registered in _LOCAL_PROVIDERS; never replaced by mock
"""
from __future__ import annotations

import re
import time
import logging
from typing import Dict, List, Tuple

from providers.base import TrustProvider

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _status(score: int) -> str:
    if score >= 7:
        return "FLAGGED"
    if score >= 4:
        return "WARNING"
    return "PASS"


# ── Injection signal groups ────────────────────────────────────────────────────
#
# Each entry is a compiled pattern that matches ONE or more signals in that group.
# Groups are scored by whether they fire at all (boolean) — not by hit count —
# to keep the scoring table simple and predictable.
#
# Patterns use VERBOSE mode for readability; the leading |/whitespace inside
# (?:...) blocks is intentional Python re-VERBOSE syntax.

_INJ_STRONG: Dict[str, re.Pattern] = {

    "override_command": re.compile(
        r"""
        (?:override|bypass|disable|turn\s+off|remove|circumvent)
            \s+
        (?:safety|filter|guardrail|restriction|limit|rule|policy|check)
        |   act\s+as\s+
            (?:
                if\s+you\s+(?:have\s+no|don.t\s+have|are\s+without)
              | an?\s+(?:unrestricted|uncensored|unfiltered|unaligned)
            )
        |   you\s+(?:have\s+no\s+restrictions?|are\s+now\s+free\s+to)
        |   do\s+anything\s+now
        |   \b(?:dan|developer)\s+mode\b
        |   jailbreak(?:ed|ing)?
        """,
        re.IGNORECASE | re.VERBOSE,
    ),

    "delimiter_injection": re.compile(
        r"""
            ```\s*(?:system|instruction|prompt|override)
        |   \#{3,}\s*(?:system|instruction|override)
        |   <\|?(?:im_start|im_end)\|?>
        |   \[INST\]|\[/INST\]|\[SYS\]|\[/SYS\]
        |   <\s*(?:system|instructions?)\s*>
        |   \{\{.*?(?:role|system|instruction).*?\}\}
        |   ={3,}\s*(?:begin|start|new)\s+(?:instruction|prompt|system)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),

    "impersonation": re.compile(
        r"""
            (?:pretend|imagine)\s+(?:you\s+are|to\s+be)\s+
            (?:anthropic|openai|the\s+(?:developer|creator|admin|system|operator))
        |   you\s+are\s+(?:now|actually|really)\s+
            .{0,30}
            (?:anthropic|gpt|openai|the\s+(?:ai|system|model))
        |   \bi\s+am\s+(?:your|the)\s+
            (?:developer|creator|admin|operator|anthropic|openai)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
}

_INJ_WEAK: Dict[str, re.Pattern] = {

    "role_boundary": re.compile(
        r"""
            ignore\s+(?:previous|prior|all|above|the)\s+
            (?:instructions?|prompts?|rules?|guidelines?|context)
        |   disregard\s+(?:your|previous|prior|all)\s+
            (?:instructions?|context|rules?)
        |   forget\s+(?:everything|all|what)\s+(?:you|i|we)\b
        |   new\s+instruction[s:]
        |   -{3,}\s*(?:new|updated?|revised?|override)\s*(?:instruction|system|context)
        |   \[?\bsystem\b\]?\s*:?\s*(?:you\s+are|your\s+role|act\s+as)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),

    "template_fragment": re.compile(
        r"""
            \$\{\s*\w+\s*\}   # ${VARIABLE} Jinja/Mustache slot
        |   \%\{[^}]+\}        # %{variable} template fragment
        |   <<\w+>>            # <<SLOT>> placeholder injection
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
}


# ── Jailbreak signal groups ────────────────────────────────────────────────────

_JBK_STRONG: Dict[str, re.Pattern] = {

    "constraint_removal": re.compile(
        r"""
            (?:pretend|imagine|assume|act\s+as\s+if)\s+
            (?:there\s+are\s+no|you\s+(?:have\s+no|don.t\s+have|are\s+without))\s+
            (?:rules?|restrictions?|limits?|guidelines?|safety|filter)
        |   without\s+any\s+(?:ethical|moral|safety|content)\s+
            (?:guidelines?|filters?|restrictions?|concerns?)
        |   no\s+(?:ethical|moral|safety|content)\s+
            (?:guidelines?|filters?|restrictions?|concerns?)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),

    "authority_claim": re.compile(
        r"""
            (?:i\s+am|this\s+is)\s+(?:your|the)\s+
            (?:developer|creator|admin|operator|anthropic|openai)
        |   anthropic\s+(?:here|staff|employee|engineer|researchers?)
        |   you\s+(?:must|have\s+to|are\s+required\s+to)\s+obey\s+
            (?:me|this|the\s+(?:user|admin|operator))
        |   confidential\s+system\s+(?:prompt|instruction|override)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),

    "persistence_command": re.compile(
        r"""
            remember\s+
            (?:this|that|for\s+all|going\s+forward|from\s+now\s+on|always)
            \s*[:.]?\s*
            (?:you\s+are|ignore|bypass|override)
        |   for\s+(?:all|every|each|future|subsequent)\s+
            (?:response|message|turn|interaction)\s*[,:]?\s*
            (?:ignore|bypass|override|disregard)
        |   until\s+i\s+say\s+otherwise
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
}

_JBK_WEAK: Dict[str, re.Pattern] = {

    "hypothetical_bypass": re.compile(
        r"""
            in\s+a\s+(?:fictional|hypothetical|imaginary|alternate)\s+
            (?:scenario|world|universe|story|context)
        |   if\s+you\s+were\s+(?:an?\s+)?
            (?:evil|unrestricted|unaligned|rogue|hacked)\s+ai
        |   let.s\s+play\s+a\s+(?:game|roleplay|scenario)\s+where\s+you
        """,
        re.IGNORECASE | re.VERBOSE,
    ),

    "research_framing": re.compile(
        r"""
            for\s+(?:research|educational|academic|fictional|hypothetical)\s+
            purposes?\s+only
        |   for\s+my\s+(?:thesis|paper|study|research|class)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
}


# ── Transfer-context amplifier ─────────────────────────────────────────────────
#
# Presence of any of these signals alongside injection/jailbreak indicators raises
# the score by +2.  These patterns target the specific vectors that make finapp
# tool calls (transfer_funds, schedule_payment) exploitable.

_TRANSFER_AMP: re.Pattern = re.compile(
    r"""
        # Account / beneficiary substitution
        (?:change|update|modify|alter|switch)\s+(?:the\s+)?
        (?:destination|recipient|beneficiary|account|payee)\s*(?:to|address|number)
    |   (?:instead\s+of|rather\s+than).{0,40}
        (?:send|transfer|pay)\s+(?:to|into)
    |   redirect.{0,40}(?:fund|payment|transfer|money)
    |   (?:new|updated?|different|alternate)\s+
        (?:account\s+number|routing\s+number|iban|bic|swift)

        # Memo / note field injection
    |   (?:memo|note|reference|message|description)
        \s*[:=]\s*.{0,15}
        (?:ignore|override|bypass|system|instruction|forget|new\s+task)
    |   memo\s*[:=]\s*['\"]?\s*
        (?:ignore|you\s+are|act\s+as|new\s+instruction)

        # Amount / limit override
    |   (?:ignore|disregard|forget)\s+the\s+
        (?:amount|limit|threshold|maximum)
    |   send\s+(?:all|the\s+(?:full|entire|whole|maximum|total))\s+
        (?:balance|amount|fund)
    |   (?:override|bypass).{0,40}(?:limit|threshold|maximum|approval)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ── Structural / linguistic heuristics ────────────────────────────────────────

_NEWLINE_INJECT = re.compile(r"(?:\\n\s*){4,}|(?:\n\s*){5,}")
_BOUNDARY_DENSE = re.compile(r"[<>{}|\[\]]{3,}")
_UNICODE_ANOMALY = re.compile(
    # Zero-width and bidirectional-control characters commonly used to hide
    # injected text.  Using explicit \u escapes so this file stays ASCII-safe
    # regardless of system locale / Python file-encoding.
    #
    # U+200B ZERO WIDTH SPACE
    # U+200C ZERO WIDTH NON-JOINER
    # U+200D ZERO WIDTH JOINER
    # U+202A-U+202E LRE / RLE / PDF / LRO / RLO (bidi embedding controls)
    # U+2066-U+2069 LRI / RLI / FSI / PDI (bidi isolate controls)
    # U+FEFF ZERO WIDTH NO-BREAK SPACE (BOM)
    "[​‌‍‪-‮⁦-⁩﻿]"
)


def _structural_hits(content: str) -> int:
    """Count distinct structural anomaly types present (0–3)."""
    hits = 0
    if _NEWLINE_INJECT.search(content):
        hits += 1
    if _BOUNDARY_DENSE.search(content):
        hits += 1
    if _UNICODE_ANOMALY.search(content):
        hits += 1
    return hits


# ── Score synthesis ────────────────────────────────────────────────────────────

def _score(
    strong_groups: Dict[str, re.Pattern],
    weak_groups:   Dict[str, re.Pattern],
    content: str,
    struct_hits: int,
    transfer_ctx: bool,
) -> Tuple[int, List[str], List[str]]:
    """
    Compute score and collect fired group names.

    Scoring table (combined = strong_hits*2 + weak_hits):
      0 → 1   PASS
      1 → 3   PASS (border, below threshold)
      2 → 5   WARNING
      3 → 7   FLAGGED
      4+ → 9  FLAGGED (maximum)

    Transfer amplifier: +2 if transfer context fires, capped at 9.

    Returns (score, strong_fired_names, weak_fired_names).
    """
    strong_fired = [g for g, p in strong_groups.items() if p.search(content)]
    weak_fired   = [g for g, p in weak_groups.items()   if p.search(content)]

    # Two or more structural anomaly types count as one extra weak hit
    if struct_hits >= 2:
        weak_fired = weak_fired + ["__structural__"]

    combined = len(strong_fired) * 2 + len(weak_fired)

    if combined == 0:
        raw = 1
    elif combined == 1:
        raw = 3
    elif combined == 2:
        raw = 5
    elif combined == 3:
        raw = 7
    else:
        raw = 9

    if transfer_ctx and raw > 1:
        raw = min(9, raw + 2)

    return raw, strong_fired, weak_fired


# ── Provider ──────────────────────────────────────────────────────────────────

class TransferSecurityProvider(TrustProvider):
    """
    In-process prompt injection and jailbreak detection with transfer-context
    amplification.

    Registered as a LOCAL_PROVIDER in GS startup — never overridden by mock_provider
    in MOCK_MODE.  Suitable for production: no credentials, no network calls,
    sub-millisecond latency.
    """

    def name(self) -> str:
        return "transfer_security_provider"

    @property
    def fail_mode(self) -> str:
        return "fail_open"

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start = time.time()
        results: List[Dict] = []

        struct_hits  = _structural_hits(content)
        transfer_ctx = bool(_TRANSFER_AMP.search(content))

        for dim in evaluations:

            if dim == "prompt_injection":
                s, strong_f, weak_f = _score(
                    _INJ_STRONG, _INJ_WEAK, content, struct_hits, transfer_ctx
                )
            elif dim == "jailbreak":
                s, strong_f, weak_f = _score(
                    _JBK_STRONG, _JBK_WEAK, content, struct_hits, transfer_ctx
                )
            else:
                logger.warning(
                    f"[TransferSecurityProvider] Dimension '{dim}' not handled "
                    f"— returning PASS"
                )
                results.append({
                    "dimension": dim,
                    "score":     1,
                    "status":    "PASS",
                    "provider_metadata": {
                        "provider":   "transfer_security_provider",
                        "in_process": True,
                        "note": f"Dimension '{dim}' is not handled by this provider",
                    },
                })
                continue

            status = _status(s)
            meta = {
                "provider":         "transfer_security_provider",
                "in_process":       True,
                "strong_groups":    strong_f,
                "weak_groups":      [g for g in weak_f if g != "__structural__"],
                "structural_hits":  struct_hits,
                "transfer_context": transfer_ctx,
            }

            if s > 1:
                logger.info(
                    f"[TransferSecurityProvider] {dim} score={s} status={status} "
                    f"role={content_role} "
                    f"strong={strong_f} weak={[g for g in weak_f if g != '__structural__']} "
                    f"struct={struct_hits} transfer={transfer_ctx}"
                )

            results.append({
                "dimension": dim,
                "score":     s,
                "status":    status,
                "provider_metadata": meta,
            })

        latency_ms = max(1, int((time.time() - start) * 1000))
        return results, latency_ms
