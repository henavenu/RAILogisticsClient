"""Observability helpers for the RAI Trust SDK."""
from .otel import GovernanceSpan, setup_otel

__all__ = ["setup_otel", "GovernanceSpan"]
