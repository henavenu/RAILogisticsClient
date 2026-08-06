"""
Mock Provider — content-aware scoring for development and testing.

Scoring strategy:
  1. If a named scenario is active (MOCK_SCENARIO env var), use its score overrides.
  2. Otherwise, scan the content text for dimension-specific keywords.
       0 keyword hits  → score 1  (PASS)
       1 keyword hit   → score 4-6 (WARNING)
       2+ keyword hits → score 7-9 (FLAGGED)

DIMENSION_KEYWORDS is the single source of truth for keyword lists — it is hardcoded
in this file.  There is no YAML file, no database lookup for signals.

SCENARIOS is also hardcoded — a dict of named demo overrides activated via
the MOCK_SCENARIO environment variable.
"""
import os
import time
import random
import logging
from typing import Dict, List, Optional, Tuple

from providers.base import TrustProvider

logger = logging.getLogger(__name__)


# ── Keyword signals ────────────────────────────────────────────────────────────
#
# For each dimension, list the substrings whose presence in the evaluated text
# drives the score upward.  Case-insensitive substring match.
#
#   0 matches → score 1  (PASS)    — content looks clean
#   1 match   → score 4-6 (WARNING) — possible concern, human review advised
#   2+ matches → score 7-9 (FLAGGED) — clear signal, likely violation
#
# If a dimension has no entry here it always scores 1 (no signal = no evidence).

DIMENSION_KEYWORDS: Dict[str, List[str]] = {

    # ── Safety ────────────────────────────────────────────────────────────────
    "hate": [
        "hate", "racist", "bigot", "inferior race", "subhuman", "slur",
        "worthless people",
    ],
    "harassment": [
        "harass", "bully", "stalk", "threaten", "intimidate",
    ],
    "sexual_content": [
        "explicit", "pornographic", "nude", "sexual act", "erotic", "lewd",
    ],
    "self_harm": [
        "suicide", "self-harm", "kill myself", "cut myself",
        "overdose", "end my life", "hurt myself",
    ],
    "violence": [
        "kill", "attack", "bomb", "shoot", "stab", "murder",
        "weapon", "assassinate", "massacre",
    ],

    # ── Security ──────────────────────────────────────────────────────────────
    "misconduct": [
        "fraud", "scam", "illegal", "corrupt", "bribe", "launder", "cheat", "embezzle",
        # Financial misconduct — churning, mis-selling, suitability violations
        "churning", "churn the portfolio", "excessive trading", "excessive commissions",
        "front-load fee", "front load fee", "entry fee", "maximize my commissions",
        "at the expense of the client", "at client expense",
        "unsuitable product", "unsuitable investment", "mis-selling", "misselling",
    ],
    # sensitive_data_disclosure: fires when an LLM response discloses regulated
    # identifiers (SSN, card numbers, credentials, routing/account numbers).
    # Maps to OWASP LLM02 — Sensitive Information Disclosure.
    # Track: COERCIVE — PE hard-blocks and returns safe refusal.
    "sensitive_data_disclosure": [
        "password", "ssn", "social security", "credit card", "api key",
        "secret key", "private key", "credentials", "bearer token", "access token",
        # Financial account identifiers — fire when an LLM RESPONSE echoes raw numbers
        # One hit → NORMATIVE warning; two or more hits → COERCIVE block
        "routing number", "account number", "aba number", "iban",
        # NOTE: balance-disclosure keywords ("account balance", "your balance", …)
        # were removed. Showing an *authenticated customer their own balance /
        # transaction history* is the core function of a self-service banking app,
        # not a regulated-data breach — they caused false-positive COERCIVE blocks
        # on the benign "Recent transactions" / balance reads. Genuine secrets
        # (SSN, cards, keys, raw account/routing numbers) above still fire.
    ],
    "sensitive_data_request": [
        # Compound verb+noun phrases only — avoids false positives on
        # legitimate questions like "what is my credit score"
        "show me my ssn", "show me my social security", "show me my account number",
        "show me my full profile", "show me my password", "show me my routing number",
        "give me my ssn", "give me my account number", "give me my password",
        "tell me my ssn", "tell me my account number", "tell me my password",
        "what is my ssn", "what is my social security", "what is my account number",
        "what is my password", "what is my routing number",
        "display my ssn", "display my account number", "display my password",
        "reveal my ssn", "reveal my password", "reveal my account",
        "my full profile", "full profile", "complete profile",
        "full ssn", "complete ssn", "my full ssn",
        "my credit card number", "my pin number",
        "all account details", "all my details", "all personal details",
    ],
    "prompt_injection": [
        # Role-boundary violations
        "ignore previous", "disregard instructions", "forget your instructions",
        "override system", "new instructions:", "ignore all prior",
        # Delimiter injection attempts
        "```system", "###system", "[inst]", "[sys]", "<system>", "<instructions>",
        # Identity impersonation
        "i am your developer", "i am anthropic", "i am the admin", "i am the operator",
        # Template/slot injection
        "${", "<<override>>", "%{inject",
    ],
    "jailbreak": [
        # Constraint removal
        "dan mode", "jailbreak", "bypass safety", "unrestricted mode",
        "pretend you have no", "ignore all constraints", "no restrictions",
        # Authority claims
        "this is anthropic", "anthropic staff", "i am your creator",
        "you must obey me", "confidential system override",
        # Hypothetical bypass framing
        "in a fictional world where you have no rules",
        "if you were an unrestricted ai",
        "let's roleplay that you have no safety",
        # Persistence commands
        "for all future responses ignore", "until i say otherwise bypass",
        "from now on disregard",
    ],
    "malicious_intent": [
        "hack", "exploit", "vulnerability", "malware", "phishing",
        "ransomware", "zero-day", "root access",
        # Financial system abuse — unauthorized transfers, bypassing controls
        "unauthorized transfer", "unauthorised transfer",
        "bypass compliance", "bypass all controls", "bypass oversight",
        "no audit trail", "without audit", "no compliance check",
        "bypass suitability", "skip compliance", "skip suitability",
        "unauthorized transaction", "fraudulent transfer",
    ],

    # groundedness — deferred to Tier 2 / Arize; kept empty so mock always
    # scores PASS (score=1), which is correct for a deferred dimension.
    "groundedness": [],

    # ── Privacy / Compliance ──────────────────────────────────────────────────
    # data_minimisation_violation: fires when an AI *response* returns more
    # personal data than the request warranted — a violation of GDPR Art. 5(1)(c)
    # (data must be "adequate, relevant and limited to what is necessary").
    # Distinct from sensitive_data_disclosure:
    #   sensitive_data_disclosure  = regulated identifiers (SSN, card) → COERCIVE block
    #   data_minimisation_violation = excess PII (address+phone+DOB combos) → NORMATIVE mask
    "data_minimisation_violation": [
        # Date of birth / age identifiers
        "date of birth:", "date of birth is", "dob:", "your dob is",
        "birthdate:", "born on", "your birth date",
        # Contact PII in responses
        "your email address is", "email address on file", "your email is",
        "your phone number is", "phone number on file", "your mobile number is",
        "your home address is", "your mailing address is", "address on file",
        "your postcode is", "your zip code is",
        # Government / national identifiers (full phrase — short forms in sensitive_data_disclosure)
        "passport number", "driver's license number", "national insurance number",
        "tax identification number", "taxpayer id", "national id number",
        "social security number is", "ssn is",
        # Health / medical PII
        "your diagnosis is", "your medical record", "your health condition",
        "your prescription is", "your medication history",
        "your medical history shows",
        # Account holder identification
        "your full name is", "name on account", "account holder name is",
        "account registered to", "legal name on file",
        # Biometric references
        "your biometric", "facial recognition data", "fingerprint on file",
    ],

    # ── Financial Crime ───────────────────────────────────────────────────────
    "sanctions_country_reference": [
        # OFAC comprehensive embargoes — full names
        "iran", "iranian", "north korea", "dprk", "cuba", "cuban",
        "syria", "syrian",
        # Russia / Belarus post-2022 sectoral blocks — full names
        "russia", "russian", "belarus", "belarusian",
        # Other FATF / UN high-risk jurisdictions — full names
        "venezuela", "venezuelan", "myanmar", "burma", "sudan", "sudanese",
        "somalia",
        # ISO 3166-1 alpha-2 codes as they appear in JSON-serialised tool args
        # (embedded quotes prevent false-positive substring matches on common
        #  letter pairs — "ir" would hit "wire", "transfer", etc.)
        '"ir"',   # Iran
        '"kp"',   # North Korea (DPRK)
        '"cu"',   # Cuba
        '"sy"',   # Syria
        '"ru"',   # Russia
        '"by"',   # Belarus
        '"ve"',   # Venezuela
        '"mm"',   # Myanmar / Burma
        '"sd"',   # Sudan
        '"so"',   # Somalia
        # Generic sanctions language
        "sanctioned country", "ofac sanctioned", "embargo country",
        "restricted country", "sanctioned jurisdiction",
    ],
    "sanctions_entity_mention": [
        "sdn list", "sdn listed", "ofac list", "blocked entity",
        "sanctioned entity", "specially designated", "blocked person",
        "sanctioned individual", "sanctioned company",
        # Named sanctioned institutions (bank names)
        "sberbank", "vtb bank", "gazprombank", "promsvyazbank",
        "bank rossiya", "sovcombank",
        # SWIFT/BIC codes for sanctioned Russian banks
        # SABRRUMM = Sberbank Russia, VTBRRUMM = VTB Bank, GAZPRUMM = Gazprombank
        # PRSBRUMM = Promsvyazbank, ROSYRUMM = Bank Rossiya, SVCBRUMM = Sovcombank
        "sabrrumm", "sabr",
        "vtbrrumm",
        "gazprumm",
        "prsbrumm",
        "rosyrumm",
        "svcbrumm",
    ],
    "sanctions_evasion_signal": [
        "avoid sanctions", "bypass screening", "avoid ofac", "avoid detection",
        "shell company", "third country routing", "layering through",
        "circumvent controls", "front company", "avoid reporting", "nominee account",
    ],
    "aml_structuring_signal": [
        "structuring", "smurfing", "split the payment", "split the transfer",
        "below the threshold", "avoid the limit", "avoid reporting threshold",
        "multiple small transfers", "just under", "break it up",
        "under 10000", "under $10,000", "stay below",
    ],

    # ── Agent Boundary / Scope Excursion ──────────────────────────────────────
    #
    # agent_boundary_violation: fires when a governed agent invokes a tool
    # outside its declared scope — e.g. a domestic ACH agent routing a payment
    # through an international SWIFT channel without authorisation.
    # Two hits → COERCIVE (GovernanceBlock) — labelled clearly as scope excursion
    # so reviewers and the Sentinel can distinguish it from a pure sanctions hit.
    "agent_boundary_violation": [
        "correspondent bank routing",
        "unauthorized",
        "cross-boundary",
        "out-of-scope",
        "agent boundary",
        "unauthorized correspondent",
        "domestic agent",
        "scope excursion",
        "unauthorized wire",
        "unauthorized transfer",
    ],

    # ── EU AI Act ─────────────────────────────────────────────────────────────
    #
    # eu_ai_act_transparency (Art. 13) — llm_response only
    # High score = AI response presents output as authoritative without
    # appropriate caveats, or conceals its AI nature.
    "eu_ai_act_transparency": [
        # AI identity concealment
        "i am not an ai", "i am a human", "i am a real person",
        "speaking as a human", "i am a person not a bot",
        # Overconfident financial/investment claims without disclaimers
        "guaranteed returns", "i guarantee", "certain profit",
        "no risk", "risk-free investment", "i promise this",
        "you cannot lose", "no possibility of loss",
        "you should definitely invest", "definitely buy this",
        "definitely sell", "this will definitely",
        "the market will definitely", "the stock will definitely",
        # Impersonating licensed professional expertise
        "as your financial advisor", "as your legal advisor",
        "as your doctor", "as your certified",
        "my professional recommendation is",
        "i am advising you as a professional",
        # Denying AI uncertainty and limitations
        "i have no limitations", "i am always accurate",
        "i am never wrong", "i am fully up to date",
        "my information is always current", "i have complete information",
        "i know everything about your account",
    ],

    # eu_ai_act_prohibited_practice (Art. 5) — all roles
    # High score = AI content uses manipulation, coercion, or exploits
    # psychological vulnerabilities contrary to EU AI Act Art. 5 prohibitions.
    "eu_ai_act_prohibited_practice": [
        # Pressure and manipulation
        "manipulate you", "pressure you", "act now or lose",
        "fear of missing out", "you must act immediately",
        "exploit your situation", "subliminal", "psychological pressure",
        # Urgency / artificial scarcity
        "limited time only", "only a few spots left",
        "act fast or miss out", "this offer expires",
        "urgent action required", "before it's too late",
        "last chance to", "don't miss out", "offer ends today",
        "opportunity closes", "window is closing",
        # Vulnerability exploitation framing
        "given your financial situation", "because you are struggling",
        "since you need money urgently", "given your debt situation",
        "you can't afford not to", "in your desperate situation",
        "knowing you are behind on", "taking advantage of your position",
        # False social proof / herding
        "everyone else is doing", "all smart investors are",
        "don't be the only one who misses", "join thousands who",
        "don't get left behind", "others are already profiting",
        # Emotional coercion
        "you'll regret not acting", "imagine losing everything",
        "your family depends on this", "protect your loved ones by acting",
        "you can't afford to wait", "failure to act will cost you",
    ],

    # eu_ai_act_bias_discrimination (Art. 10) — llm_response only
    # High score = AI response provides differential treatment or makes
    # assumptions based on protected characteristics.
    "eu_ai_act_bias_discrimination": [
        # Generic demographic framing
        "people like you", "your demographic", "based on your age",
        "women are typically", "men are typically", "elderly people should",
        "young people usually", "that group tends to",
        # Protected characteristics used as decision basis
        "based on your race", "based on your ethnicity",
        "because of your religion", "based on your gender",
        "due to your nationality", "people of your background",
        "based on your sexual orientation", "based on your disability",
        # Differential treatment / stereotyping
        "for someone like you", "people from your community typically",
        "given your cultural background", "people in your situation usually",
        "culturally, your people tend to", "traditionally, that group",
        # Geographic/socioeconomic proxies (financial discrimination)
        "people in your neighborhood tend to", "based on your zip code",
        "from that type of area, people", "your type of community usually",
        # Age-based discrimination
        "at your age you should not", "someone your age shouldn't",
        "too old to take that risk", "too young to understand",
        # Discriminatory risk framing
        "that demographic defaults more", "that group tends to miss payments",
        "clients from that background are higher risk",
    ],

    # ── Agent Coordination — Payment Surge Council (advisory NORMATIVE-only) ────
    # Keywords are deliberately DISJOINT from aml_structuring_signal /
    # sanctions_* / malicious_intent so a council round never trips a COERCIVE
    # dimension. Each council agent's A2A prompt carries its dimension's phrases.
    "fraud_velocity_signal": [
        "transaction velocity", "velocity anomaly", "velocity spike",
        "fraud risk score", "anomalous account behavior", "elevated transaction rate",
        "velocity threshold exceeded",
    ],
    "revenue_override_signal": [
        "lift the fraud hold", "release flagged payments", "approve despite the fraud flag",
        "expected holiday volume", "projected revenue loss", "revenue at risk",
        "approve the flagged batch",
    ],
    "cx_friction_signal": [
        "false decline", "false declines", "step-up verification",
        "support friction", "decline rate", "customer churn",
        "post-transaction monitoring",
    ],
    "risk_exposure_signal": [
        "ctr filing obligation", "regulatory exposure", "sar review",
        "cumulative batch value", "compliance exposure", "financial exposure",
        "cross-border concentration",
    ],
}

# ── Response-only dimensions ──────────────────────────────────────────────────
#
# These dimensions detect LLM *output* leaking sensitive data.  Evaluating them
# on user_prompt or tool_call produces false positives: users legitimately include
# account codes in their requests, and tool arguments legitimately carry them.
# When content_role is not 'llm_response' these dimensions always score 1 (PASS).

_RESPONSE_ONLY_DIMENSIONS: frozenset = frozenset({
    "sensitive_data_disclosure",
    "data_minimisation_violation",
})


# ── Score bands ────────────────────────────────────────────────────────────────
#
# Inclusive ranges for random.randint.
# zero_hits: clean content used to always score exactly 1, which made every Eval-Trail
# row a wall of 1s and read as contrived. Instead, give each dimension a small
# DETERMINISTIC clean baseline in [1, min(3, permissive-ceiling)] — scores vary 1/2/3
# across dimensions while STILL landing inside each dimension's PERMISSIVE band, so no
# clean evaluation is ever falsely flipped to NORMATIVE.
BANDS = {
    "one_hit":   [4, 6],
    "multi_hit": [7, 9],
}

# Dimensions whose PERMISSIVE band tops out below 3 (from config_policies). Their clean
# score must be capped to the ceiling; everything else may reach 3. Keep in sync with
# the policy thresholds — these are the only zero/low-tolerance dims in the catalog.
_PERMISSIVE_CEIL = {
    "data_minimisation_violation":  1,   # zero-tolerance
    "sanctions_country_reference":  1,
    "sanctions_entity_mention":     1,
    "sanctions_evasion_signal":     1,
    "aml_structuring_signal":       2,
    "eu_ai_act_transparency":       2,
    "session_risk_scorer":          2,
}


def _clean_score(dimension: str) -> int:
    """Stable per-dimension clean baseline in [1, min(3, permissive-ceiling)]."""
    ceil = min(3, _PERMISSIVE_CEIL.get(dimension, 3))
    if ceil <= 1:
        return 1
    return 1 + (sum(dimension.encode()) % ceil)   # deterministic, stable across restarts

# Advisory-only dimensions (Payment Surge Council): on ANY keyword hit they score
# a deterministic 5 (NORMATIVE) and never escalate to FLAGGED/COERCIVE — regardless
# of hit count or threshold profile. This guarantees the multi-agent deliberation
# runs to completion (no single agent hard-blocks); the cross-turn conflict is
# escalated by Sentinel, not by a per-dimension verdict.
_ADVISORY_ONLY_DIMENSIONS = {
    "fraud_velocity_signal",
    "revenue_override_signal",
    "cx_friction_signal",
    "risk_exposure_signal",
}
_ADVISORY_SCORE = 5   # NORMATIVE under standard/strict profiles and config_policies

# ── Named scenarios ────────────────────────────────────────────────────────────
#
# Activated via MOCK_SCENARIO env var.
# override_all: applies to every dimension not listed in overrides.
# overrides:    per-dimension score/status, takes precedence over override_all.
#
# Usage:  MOCK_SCENARIO=wire_transfer_sanctions

SCENARIOS: Dict[str, dict] = {
    "clean": {
        "description": "All content passes — green-path baseline",
        "override_all": {"score": 1, "status": "PASS"},
    },
    "borderline": {
        "description": "Mixed signals — NORMATIVE verdict demo",
        "overrides": {
            "violence":   {"score": 5, "status": "WARNING"},
            "misconduct": {"score": 6, "status": "WARNING"},
        },
    },
    "data_breach": {
        "description": "Regulated identifier disclosure (SSN / credentials) — COERCIVE block",
        "overrides": {
            "sensitive_data_disclosure": {"score": 8, "status": "FLAGGED"},
        },
    },
    "minimisation_violation": {
        "description": "Excess PII in response — data minimisation violation — NORMATIVE mask",
        "overrides": {
            "data_minimisation_violation": {"score": 7, "status": "FLAGGED"},
        },
    },
    "hate_speech": {
        "description": "Hate speech — COERCIVE verdict demo",
        "overrides": {
            "hate":       {"score": 8, "status": "FLAGGED"},
            "harassment": {"score": 7, "status": "FLAGGED"},
        },
    },
    "multi_violation": {
        "description": "Multiple high-severity violations",
        "overrides": {
            "hate":                     {"score": 9, "status": "FLAGGED"},
            "sensitive_data_disclosure": {"score": 8, "status": "FLAGGED"},
            "violence":                 {"score": 7, "status": "FLAGGED"},
        },
    },
    "wire_transfer_aml": {
        "description": "AML evasion signals on wire transfer — NORMATIVE sanctions demo",
        "overrides": {
            "sanctions_evasion_signal": {"score": 6, "status": "WARNING"},
            "aml_structuring_signal":   {"score": 5, "status": "WARNING"},
        },
    },
    "wire_transfer_sanctions": {
        "description": "Sanctioned jurisdiction + entity hit — COERCIVE sanctions demo",
        "overrides": {
            "sanctions_country_reference": {"score": 8, "status": "FLAGGED"},
            "sanctions_entity_mention":    {"score": 7, "status": "FLAGGED"},
            "sanctions_evasion_signal":    {"score": 5, "status": "WARNING"},
        },
    },
    "russia_transfer": {
        "description": "Russia-linked transfer — COERCIVE block (post-2022 sectoral)",
        "overrides": {
            "sanctions_country_reference": {"score": 9, "status": "FLAGGED"},
            "sanctions_entity_mention":    {"score": 7, "status": "FLAGGED"},
            "sanctions_evasion_signal":    {"score": 5, "status": "WARNING"},
        },
    },
    "aml_structuring": {
        "description": "Amount structuring to avoid reporting thresholds",
        "overrides": {
            "aml_structuring_signal":   {"score": 7, "status": "FLAGGED"},
            "sanctions_evasion_signal": {"score": 5, "status": "WARNING"},
        },
    },
    "full_financial_crime": {
        "description": "Maximum financial crime signal — all four dims at high scores",
        "overrides": {
            "sanctions_country_reference": {"score": 9, "status": "FLAGGED"},
            "sanctions_entity_mention":    {"score": 8, "status": "FLAGGED"},
            "sanctions_evasion_signal":    {"score": 7, "status": "FLAGGED"},
            "aml_structuring_signal":      {"score": 7, "status": "FLAGGED"},
        },
    },
    "eu_ai_act_transparency_fail": {
        "description": "AI overconfident advice — transparency violation Art. 13",
        "overrides": {
            "eu_ai_act_transparency": {"score": 7, "status": "FLAGGED"},
        },
    },
    "eu_ai_act_prohibited": {
        "description": "Manipulative language — Art. 5 prohibited practice",
        "overrides": {
            "eu_ai_act_prohibited_practice": {"score": 8, "status": "FLAGGED"},
            "eu_ai_act_transparency":        {"score": 5, "status": "WARNING"},
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_for_score(score: int) -> str:
    if score >= 7:
        return "FLAGGED"
    if score >= 4:
        return "WARNING"
    return "PASS"


# ── Provider ──────────────────────────────────────────────────────────────────

class MockProvider(TrustProvider):
    """
    Content-aware mock provider for development and testing.

    All configuration (signals and scenarios) is hardcoded in this file.
    No YAML files, no database lookups — behaviour is completely predictable.
    """

    def __init__(self):
        # Nothing to load — DIMENSION_KEYWORDS and SCENARIOS are module-level constants.
        logger.info(
            f"[MockProvider] Ready — "
            f"{len(DIMENSION_KEYWORDS)} dimensions, {len(SCENARIOS)} named scenarios"
        )

    def name(self) -> str:
        return "mock_provider"

    def reload(self) -> None:
        """No-op — signals and scenarios are compile-time constants."""
        logger.info("[MockProvider] reload() called — nothing to reload (hardcoded config)")

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _active_scenario(self) -> Optional[dict]:
        name = os.getenv("MOCK_SCENARIO", "").strip().lower()
        return SCENARIOS.get(name)

    def _score_from_content(self, content: str, dimension: str, content_role: str = "") -> int:
        # Response-only dimensions must not fire on user_prompt or tool_call content.
        if dimension in _RESPONSE_ONLY_DIMENSIONS and content_role not in ("llm_response", "tool_result", ""):
            return 1

        keywords     = DIMENSION_KEYWORDS.get(dimension, [])
        content_low  = content.lower()
        hits         = sum(1 for kw in keywords if kw in content_low)

        # Advisory council dims: deterministic NORMATIVE on any hit, never COERCIVE.
        if dimension in _ADVISORY_ONLY_DIMENSIONS:
            return _ADVISORY_SCORE if hits >= 1 else 1

        if hits == 0:
            return _clean_score(dimension)          # varied 1..3, capped to permissive band
        elif hits == 1:
            lo, hi = BANDS["one_hit"]
        else:
            lo, hi = BANDS["multi_hit"]

        return random.randint(lo, hi)

    # ── SPI implementation ────────────────────────────────────────────────────

    def _evaluate(
        self,
        content: str,
        evaluations: List[str],
        content_role: str,
    ) -> Tuple[List[Dict], int]:
        start    = time.time()
        scenario = self._active_scenario()

        if scenario:
            logger.debug(
                f"[MockProvider] scenario='{os.getenv('MOCK_SCENARIO')}' "
                f"({scenario.get('description', '')})"
            )

        results = []
        for dim in evaluations:
            if scenario:
                override = (scenario.get("overrides") or {}).get(dim)
                fallback = scenario.get("override_all")
                if override:
                    score, status = override["score"], override["status"]
                elif fallback:
                    score, status = fallback["score"], fallback["status"]
                else:
                    score  = self._score_from_content(content, dim, content_role)
                    status = _status_for_score(score)
            else:
                score  = self._score_from_content(content, dim, content_role)
                status = _status_for_score(score)

            results.append({
                "dimension": dim,
                "score":     score,
                "status":    status,
                "provider_metadata": {
                    "mock":     True,
                    "scenario": os.getenv("MOCK_SCENARIO") or None,
                },
            })
            logger.debug(f"  [mock] {dim:<30} score={score} status={status}")

        latency_ms = max(10, int((time.time() - start) * 1000))
        return results, latency_ms
