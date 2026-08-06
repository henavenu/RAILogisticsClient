"""
Provider Registry — manages registered TrustProvider instances.
"""
import logging
from typing import Dict, List, Optional

from providers.base import TrustProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """
    Central registry for TrustProvider SPI implementations.
    Provider name() must be stable and unique across restarts.
    """

    def __init__(self):
        self._providers: Dict[str, TrustProvider] = {}

    def register(self, provider: TrustProvider):
        name = provider.name()
        self._providers[name] = provider
        logger.info(f"Registered provider: {name}")

    def get(self, name: str) -> Optional[TrustProvider]:
        return self._providers.get(name)

    def all_providers(self) -> Dict[str, TrustProvider]:
        return dict(self._providers)

    def provider_names(self) -> List[str]:
        return list(self._providers.keys())

    def as_catalog(self) -> List[dict]:
        """Serialisable list — used by GET /v1/providers."""
        return [
            {"provider": name, "status": "active"}
            for name in self._providers
        ]
