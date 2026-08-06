"""
Non-Human Identity (NHI) abstraction for the RAI Trust SDK.

AgentIdentity is a Protocol — any object that satisfies the interface works,
no inheritance required.  Planned concrete implementations:

  MockIdentity     — dev/test; no real credentials
  SpiffeIdentity   — SPIFFE/SPIRE x.509 SVID
  SmallstepIdentity — step-ca mTLS short-lived cert
  OidcIdentity     — OIDC client-credentials token exchange

The Trust Platform receives agent_id in the evaluate payload and (in future)
validates the bearer token / mTLS cert supplied in the request headers.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class AgentIdentity(Protocol):
    """
    Pluggable NHI abstraction.

    Implementors supply:
      • agent_id  — stable identifier for the agent (included in PE payload)
      • get_token — bearer token for the current credential (may be None)
      • get_headers — HTTP headers to attach to every Trust Platform call
    """

    @property
    def agent_id(self) -> str:
        """Stable identifier for this agent."""
        ...

    def get_token(self) -> Optional[str]:
        """
        Current bearer token, or None if not applicable.
        Implementations must handle token rotation transparently.
        """
        ...

    def get_headers(self) -> dict:
        """HTTP headers to merge into Trust Platform requests."""
        ...


# ── Built-in implementations ───────────────────────────────────────────────────

class MockIdentity:
    """
    Development / test identity.  No real credentials — never use in production.

    Usage:
        identity = MockIdentity(agent_id="finapp-agent-v1")
        client = TrustClient(..., identity=identity)
    """

    def __init__(self, agent_id: str = "mock-agent"):
        self._agent_id = agent_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def get_token(self) -> Optional[str]:
        return None

    def get_headers(self) -> dict:
        return {"X-Agent-Id": self._agent_id}

    def __repr__(self) -> str:
        return f"MockIdentity(agent_id={self._agent_id!r})"
