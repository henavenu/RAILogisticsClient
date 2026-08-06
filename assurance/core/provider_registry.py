from __future__ import annotations

from core.providers.base import AssuranceProvider


class ProviderRegistry:

    def __init__(self) -> None:
        self._registry: dict[str, type[AssuranceProvider]] = {}

    def register(self, name: str, cls: type[AssuranceProvider]) -> None:
        self._registry[name] = cls

    def get(self, name: str) -> AssuranceProvider:
        if name not in self._registry:
            raise ValueError(f"Unknown provider '{name}'. Registered: {self.available()}")
        return self._registry[name]()

    def get_active(self) -> AssuranceProvider:
        from core.config import settings
        return self.get(settings.assurance_provider)

    def available(self) -> list[str]:
        return list(self._registry)


registry = ProviderRegistry()

from core.providers.arize_provider import ArizeProvider
from core.providers.local_provider import LocalProvider
from core.providers.mock_provider import MockProvider

registry.register("arize",  ArizeProvider)
registry.register("local",  LocalProvider)
registry.register("mock",   MockProvider)

# Future providers — register here when implemented:
# registry.register("phoenix",       PhoenixProvider)       # open-source local tracing
# registry.register("datadog",       DatadogProvider)       # Datadog APM
# registry.register("azure_monitor", AzureMonitorProvider)  # Azure Monitor
# registry.register("aws_xray",      AwsXRayProvider)       # AWS X-Ray
