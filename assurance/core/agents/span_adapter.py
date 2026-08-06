from __future__ import annotations

from typing import Any


_DECISION_MAP = {
    "approve": "Approved",
    "approved": "Approved",
    "deny": "Denied",
    "denied": "Denied",
    "review": "Review",
}


def _normalize_decision(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return _DECISION_MAP.get(s, str(raw))


def spans_to_records(spans: list[dict]) -> list[dict]:
    """Map Arize loan_decision spans to BiasDetectionAgent record dicts.

    Each span's `attributes` dict carries `rai.gender`, `rai.age`, `rai.decision`,
    and optionally `rai.race`. Spans missing the decision attribute are dropped.
    """
    records: list[dict] = []
    for i, s in enumerate(spans):
        if (s.get("name") or "").lower() != "loan_decision":
            continue
        attrs = s.get("attributes") or {}
        decision = _normalize_decision(attrs.get("rai.decision"))
        if decision is None:
            continue
        rec: dict[str, Any] = {"id": f"S{i}", "decision": decision}
        if "rai.gender" in attrs:
            rec["gender"] = str(attrs["rai.gender"]).strip().title()
        if "rai.age" in attrs and attrs["rai.age"] not in ("", None):
            try:
                rec["age"] = int(float(attrs["rai.age"]))
            except (TypeError, ValueError):
                pass
        if "rai.race" in attrs:
            rec["race"] = str(attrs["rai.race"])
        records.append(rec)
    return records
