"""
rai_trust_sdk.privacy — plug & play privacy proxy layer.

Two provider implementations share the same .process(text) → ProxyResult
interface, making them drop-in replacements for each other and for the
privacy_proxy= parameter of instrument().

  PrivacyProxy         — regex + entity-name tokenization (zero ML deps)
  PresidioPrivacyProxy — Presidio NLP + regex + entity-name tokenization

Use create_proxy() to select a provider by name:

    from rai_trust_sdk.privacy import create_proxy, ProxyConfig

    # Zero-dependency regex provider (default)
    proxy = create_proxy("regex", ProxyConfig(project_names=["Atlas"]))

    # NLP provider — requires: pip install rai-trust-sdk[presidio]
    #                          python -m spacy download en_core_web_lg
    proxy = create_proxy("presidio", ProxyConfig(project_names=["Atlas"]))

    # Pass either directly to instrument():
    from rai_trust_sdk.langgraph import instrument
    app = instrument(app, ..., privacy_proxy=proxy)
"""
from __future__ import annotations

from typing import Optional

from .proxy          import PrivacyProxy, ProxyConfig, ProxyResult
from .presidio_proxy import PresidioPrivacyProxy

__all__ = [
    "PrivacyProxy",
    "PresidioPrivacyProxy",
    "ProxyConfig",
    "ProxyResult",
    "create_proxy",
]


def create_proxy(
    provider: str = "regex",
    config: Optional[ProxyConfig] = None,
) -> "PrivacyProxy | PresidioPrivacyProxy":
    """
    Factory: create a privacy proxy from a provider name.

    Parameters
    ──────────
    provider : str
        ``"regex"``    — PrivacyProxy (default)
                         Pure Python regex patterns + entity tokenization.
                         Zero extra dependencies.  Best for environments where
                         ML packages cannot be installed or latency must be
                         sub-millisecond.

        ``"presidio"`` — PresidioPrivacyProxy
                         Microsoft Presidio NLP (spaCy NER) + credential regex
                         + entity tokenization.  Catches person names, org names,
                         IBANs, free-form SSN variations, and other PII that
                         regex cannot reliably detect.
                         Requires: pip install rai-trust-sdk[presidio]
                                   python -m spacy download en_core_web_lg

    config : ProxyConfig, optional
        Proxy configuration (project names, client names, etc.).
        Defaults to ProxyConfig() (no entity lists, classifier disabled).

    Returns
    ───────
    An object with a ``.process(text: str) → ProxyResult`` interface,
    suitable for passing to ``instrument(privacy_proxy=...)``.

    Examples
    ────────
    ::

        # Regex (default) — works out of the box
        proxy = create_proxy()

        # Regex with entity lists
        proxy = create_proxy("regex", ProxyConfig(
            project_names=["Project Titan"],
            client_names=["Acme Corp"],
        ))

        # Presidio NLP — install extras first
        proxy = create_proxy("presidio", ProxyConfig(
            project_names=["Project Titan"],
        ))

    Raises
    ──────
    ValueError
        If *provider* is not one of the recognised provider names.
    """
    cfg = config or ProxyConfig()

    if provider == "regex":
        return PrivacyProxy(cfg)

    if provider == "presidio":
        return PresidioPrivacyProxy(cfg)

    raise ValueError(
        f"Unknown privacy proxy provider: {provider!r}. "
        f"Valid values are: 'regex', 'presidio'."
    )
