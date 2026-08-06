from __future__ import annotations

import os

from core.agents.base import BiasDetectionProvider


class BiasAgentRegistry:

    def __init__(self) -> None:
        self._registry: dict[str, type[BiasDetectionProvider]] = {}

    def register(self, name: str, cls: type[BiasDetectionProvider]) -> None:
        self._registry[name] = cls

    def get(self, name: str) -> BiasDetectionProvider:
        if name not in self._registry:
            raise ValueError(f"Unknown bias agent provider '{name}'. Registered: {self.available()}")
        return self._registry[name]()

    def get_active(self) -> BiasDetectionProvider:
        return self.get(os.getenv("BIAS_AGENT_PROVIDER", "mock"))

    def available(self) -> list[str]:
        return list(self._registry)


bias_agents = BiasAgentRegistry()

from core.agents.mock_provider import MockBiasProvider

bias_agents.register("mock", MockBiasProvider)

# Future providers — register here when implemented:
# bias_agents.register("statistical", StatisticalBiasProvider)   # in-house tier-2 metrics
# bias_agents.register("phoenix",     PhoenixLLMBiasProvider)    # LLM-as-judge
