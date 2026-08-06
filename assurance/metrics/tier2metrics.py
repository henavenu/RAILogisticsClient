from __future__ import annotations

import statistics

from core.models.metric_result import MetricResult
from core.provider_registry import registry

PROJECT = "rai-assurance"


def _attrs(spans: list[dict]) -> list[dict]:
    return [s["attributes"] for s in spans]


def _disparity(rates: dict[str, float]) -> float:
    vals = list(rates.values())
    return round(max(vals) - min(vals), 3) if len(vals) > 1 else 0.0


def _latency_ms(spans: list[dict]) -> list[float]:
    return [
        (s["end_time"] - s["start_time"]) / 1_000_000
        for s in spans
        if s.get("end_time") is not None and s.get("start_time") is not None
    ]


def _no_data(name: str, project: str) -> MetricResult:
    return MetricResult(metric_id=name, value=0.0, project_name=project, span_count=0)


# ── Fairness ──────────────────────────────────────────────────────────────────

def approval_rate_disparity_by_gender(spans: list[dict], project: str) -> MetricResult:
    by: dict[str, dict] = {}
    for a in _attrs(spans):
        g = a.get("rai.applicant.gender", "unknown")
        b = by.setdefault(g, {"total": 0, "approved": 0})
        b["total"] += 1
        if a.get("rai.decision") == "approved":
            b["approved"] += 1
    rates = {g: round(v["approved"] / v["total"], 2) for g, v in by.items() if v["total"]}
    disp = _disparity(rates)
    return MetricResult(
        metric_id="approval_rate_disparity_by_gender", value=disp, unit="disparity",
        project_name=project, span_count=len(spans), breakdown=rates,
    )


def denial_rate_disparity_by_gender(spans: list[dict], project: str) -> MetricResult:
    by: dict[str, dict] = {}
    for a in _attrs(spans):
        g = a.get("rai.applicant.gender", "unknown")
        b = by.setdefault(g, {"total": 0, "denied": 0})
        b["total"] += 1
        if a.get("rai.decision") == "denied":
            b["denied"] += 1
    rates = {g: round(v["denied"] / v["total"], 2) for g, v in by.items() if v["total"]}
    disp = _disparity(rates)
    return MetricResult(
        metric_id="denial_rate_disparity_by_gender", value=disp, unit="disparity",
        project_name=project, span_count=len(spans), breakdown=rates,
    )


def risk_score_disparity_by_gender(spans: list[dict], project: str) -> MetricResult:
    by: dict[str, list] = {}
    for a in _attrs(spans):
        g = a.get("rai.applicant.gender", "unknown")
        if (score := a.get("rai.risk_score")) is not None:
            by.setdefault(g, []).append(float(score))
    avgs = {g: round(statistics.mean(v), 1) for g, v in by.items() if v}
    disp = _disparity(avgs)
    return MetricResult(
        metric_id="risk_score_disparity_by_gender", value=disp, unit="points",
        project_name=project, span_count=len(spans), breakdown=avgs,
    )


def approval_rate_disparity_by_age(spans: list[dict], project: str) -> MetricResult:
    def _bucket(age: int) -> str:
        if age <= 30: return "18-30"
        if age <= 45: return "31-45"
        return "46+"

    by: dict[str, dict] = {}
    for a in _attrs(spans):
        if (age := a.get("rai.applicant.age")) is None:
            continue
        g = _bucket(int(age))
        b = by.setdefault(g, {"total": 0, "approved": 0})
        b["total"] += 1
        if a.get("rai.decision") == "approved":
            b["approved"] += 1
    rates = {g: round(v["approved"] / v["total"], 2) for g, v in by.items() if v["total"]}
    disp = _disparity(rates)
    return MetricResult(
        metric_id="approval_rate_disparity_by_age", value=disp, unit="disparity",
        project_name=project, span_count=len(spans), breakdown=rates,
    )


# ── Safety ────────────────────────────────────────────────────────────────────

def toxicity_rate(spans: list[dict], project: str) -> MetricResult:
    if not spans:
        return _no_data("toxicity_rate", project)
    flagged = sum(1 for a in _attrs(spans) if a.get("rai.toxicity_flagged"))
    rate = round(flagged / len(spans), 3)
    return MetricResult(
        metric_id="toxicity_rate", value=rate, unit="fraction",
        project_name=project, span_count=len(spans),
        breakdown={"flagged": flagged, "clean": len(spans) - flagged},
    )


def offensive_content_rate(spans: list[dict], project: str) -> MetricResult:
    if not spans:
        return _no_data("offensive_content_rate", project)
    offensive = sum(1 for a in _attrs(spans) if a.get("rai.safety_category") == "offensive")
    rate = round(offensive / len(spans), 3)
    return MetricResult(
        metric_id="offensive_content_rate", value=rate, unit="fraction",
        project_name=project, span_count=len(spans),
        breakdown={"offensive": offensive, "safe": len(spans) - offensive},
    )


def escalation_rate(spans: list[dict], project: str) -> MetricResult:
    if not spans:
        return _no_data("escalation_rate", project)
    escalated = sum(1 for a in _attrs(spans) if a.get("rai.intent") == "escalation")
    rate = round(escalated / len(spans), 3)
    return MetricResult(
        metric_id="escalation_rate", value=rate, unit="fraction",
        project_name=project, span_count=len(spans),
        breakdown={"escalated": escalated, "normal": len(spans) - escalated},
    )


def negative_sentiment_rate(spans: list[dict], project: str) -> MetricResult:
    if not spans:
        return _no_data("negative_sentiment_rate", project)
    negative = sum(1 for a in _attrs(spans) if a.get("rai.sentiment") == "negative")
    rate = round(negative / len(spans), 3)
    by_intent: dict[str, int] = {}
    for a in _attrs(spans):
        intent = a.get("rai.intent", "unknown")
        by_intent[intent] = by_intent.get(intent, 0) + 1
    return MetricResult(
        metric_id="negative_sentiment_rate", value=rate, unit="fraction",
        project_name=project, span_count=len(spans), breakdown=by_intent,
    )


# ── Reliability ───────────────────────────────────────────────────────────────

def decisioning_latency_p50_ms(spans: list[dict], project: str) -> MetricResult:
    lats = _latency_ms(spans)
    if not lats:
        return _no_data("decisioning_latency_p50_ms", project)
    p50 = round(statistics.median(lats), 1)
    return MetricResult(
        metric_id="decisioning_latency_p50_ms", value=p50, unit="ms",
        project_name=project, span_count=len(spans),
        breakdown={"p50": p50, "min": round(min(lats), 1), "max": round(max(lats), 1)},
    )


def conversational_latency_p50_ms(spans: list[dict], project: str) -> MetricResult:
    lats = _latency_ms(spans)
    if not lats:
        return _no_data("conversational_latency_p50_ms", project)
    p50 = round(statistics.median(lats), 1)
    return MetricResult(
        metric_id="conversational_latency_p50_ms", value=p50, unit="ms",
        project_name=project, span_count=len(spans),
        breakdown={"p50": p50, "min": round(min(lats), 1), "max": round(max(lats), 1)},
    )


def decisioning_score_consistency(spans: list[dict], project: str) -> MetricResult:
    scores = [float(a["rai.risk_score"]) for a in _attrs(spans) if "rai.risk_score" in a]
    if len(scores) < 2:
        return _no_data("decisioning_score_consistency", project)
    std = round(statistics.stdev(scores), 2)
    return MetricResult(
        metric_id="decisioning_score_consistency", value=std, unit="stdev",
        project_name=project, span_count=len(spans),
        breakdown={"mean": round(statistics.mean(scores), 1), "stdev": std},
    )


def conversational_safe_interaction_rate(spans: list[dict], project: str) -> MetricResult:
    if not spans:
        return _no_data("conversational_safe_interaction_rate", project)
    safe = sum(
        1 for a in _attrs(spans)
        if a.get("rai.safety_category") == "safe" and not a.get("rai.toxicity_flagged")
    )
    rate = round(safe / len(spans), 3)
    return MetricResult(
        metric_id="conversational_safe_interaction_rate", value=rate, unit="fraction",
        project_name=project, span_count=len(spans),
        breakdown={"safe": safe, "unsafe": len(spans) - safe},
    )


# ── Metric lists ──────────────────────────────────────────────────────────────

_DECISIONING_METRICS = [
    approval_rate_disparity_by_gender,
    denial_rate_disparity_by_gender,
    risk_score_disparity_by_gender,
    approval_rate_disparity_by_age,
    decisioning_latency_p50_ms,
    decisioning_score_consistency,
]

_CONVERSATIONAL_METRICS = [
    toxicity_rate,
    offensive_content_rate,
    escalation_rate,
    negative_sentiment_rate,
    conversational_latency_p50_ms,
    conversational_safe_interaction_rate,
]


# ── Cycle + scheduler ─────────────────────────────────────────────────────────

def run_cycle(project: str = PROJECT, since_hours: int = 1) -> list[MetricResult]:
    from evals.evals import run_evals

    provider = registry.get_active()

    decisioning_spans    = provider.fetch_spans(project, "loan_decision", since_hours=since_hours)
    conversational_spans = provider.fetch_spans(project, "support_chat",  since_hours=since_hours)
    all_spans            = decisioning_spans + conversational_spans

    results: list[MetricResult] = []

    for fn in _DECISIONING_METRICS:
        results.append(fn(decisioning_spans, project))

    for fn in _CONVERSATIONAL_METRICS:
        results.append(fn(conversational_spans, project))

    for r in run_evals(all_spans, project):
        results.append(r)

    return results


def start_scheduler(project: str = PROJECT, interval_minutes: int = 15) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()
    scheduler.add_job(run_cycle, "interval", minutes=interval_minutes, args=[project])
    print(f"Scheduler started — every {interval_minutes} min. Ctrl+C to stop.")
    scheduler.start()
