from __future__ import annotations

from abc import ABC, abstractmethod


class AssuranceProvider(ABC):

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the backend is reachable and configured."""

    @abstractmethod
    def fetch_spans(
        self,
        project_name: str,
        use_case: str,
        since_hours: int = 1,
    ) -> list[dict]:
        """Return spans for the given use_case within the last since_hours hours.

        Each dict must contain at minimum:
          - name (str)
          - attributes (dict)
          - start_time (int, nanoseconds epoch)
          - end_time (int, nanoseconds epoch)
        """
