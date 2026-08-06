from __future__ import annotations

import httpx
import streamlit as st

BASE_URL = "http://localhost:8000"

st.set_page_config(
    page_title="RAI Assurance Dashboard",
    page_icon="🛡️",
    layout="wide",
)


def _fetch(path: str) -> dict | None:
    try:
        r = httpx.get(f"{BASE_URL}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Cannot reach API at {BASE_URL}{path} — is uvicorn running?\n\n{e}")
        return None


def _run_cycle() -> dict | None:
    try:
        r = httpx.post(f"{BASE_URL}/jobs/run", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Failed to trigger metrics cycle: {e}")
        return None


st.title("🛡️ RAI Assurance Dashboard")
st.caption("Responsible AI — Fairness · Safety · Reliability · Privacy (raw metric view; verdicts live in PAP)")

col_status, col_refresh, col_run = st.columns([4, 1, 1])

with col_refresh:
    refresh = st.button("↻ Refresh", use_container_width=True)

with col_run:
    recompute = st.button("⚡ Recompute", use_container_width=True)

if recompute:
    with st.spinner("Running Tier-2 metrics cycle..."):
        result = _run_cycle()
    if result:
        st.success(f"Computed {result['metrics_computed']} metrics.")

summary_data = _fetch("/metrics/summary")
metrics_data = _fetch("/metrics")

if not summary_data or not metrics_data:
    st.stop()

total_metrics = summary_data["total_metrics"]
by_name       = {m["metric_id"]: m for m in metrics_data.get("metrics", [])}

with col_status:
    st.markdown(
        f"<div style='background:#374151;padding:10px 18px;border-radius:8px;"
        f"color:white;font-size:1.1rem;font-weight:600'>"
        f"Live metrics: {total_metrics} computed</div>",
        unsafe_allow_html=True,
    )

st.divider()

PILLARS = {
    "fairness": {
        "label": "Fairness",
        "icon": "⚖️",
        "chart_metric": "approval_rate_disparity_by_gender",
        "chart_title": "Approval Rate by Gender",
        "metrics": [
            "approval_rate_disparity_by_gender",
            "denial_rate_disparity_by_gender",
            "risk_score_disparity_by_gender",
            "approval_rate_disparity_by_age",
        ],
    },
    "safety": {
        "label": "Safety",
        "icon": "🔒",
        "chart_metric": "toxicity_rate",
        "chart_title": "Support Chat Breakdown",
        "metrics": [
            "toxicity_rate",
            "offensive_content_rate",
            "escalation_rate",
            "negative_sentiment_rate",
        ],
    },
    "reliability": {
        "label": "Reliability",
        "icon": "⚙️",
        "chart_metric": "decisioning_latency_p50_ms",
        "chart_title": "Latency p50 (ms)",
        "metrics": [
            "decisioning_latency_p50_ms",
            "conversational_latency_p50_ms",
            "decisioning_score_consistency",
            "conversational_safe_interaction_rate",
        ],
    },
    "privacy": {
        "label": "Privacy",
        "icon": "🔐",
        "chart_metric": "pii_detection_rate",
        "chart_title": "PII Detection Breakdown",
        "metrics": [
            "pii_detection_rate",
        ],
    },
}

cols = st.columns(4)

for col, (pillar_key, pillar) in zip(cols, PILLARS.items()):
    pdata = summary_data["pillars"].get(pillar_key, {})

    with col:
        st.subheader(f"{pillar['icon']} {pillar['label']}")

        chart_m = by_name.get(pillar["chart_metric"])
        if chart_m and chart_m.get("breakdown"):
            bd = chart_m["breakdown"]
            st.caption(pillar["chart_title"])
            st.bar_chart(bd, use_container_width=True)

        for name in pillar["metrics"]:
            m = by_name.get(name)
            if not m:
                st.markdown(f"- `{name}` — no data")
                continue
            val  = m["value"]
            unit = m.get("unit", "")
            st.markdown(
                f"**{name}**  \n"
                f"&nbsp;&nbsp;&nbsp;&nbsp;`{val} {unit}`",
                unsafe_allow_html=True,
            )

st.divider()
st.caption("Auto-refreshes every 30 seconds")
st.markdown(
    """
    <script>
    setTimeout(function() { window.location.reload(); }, 30000);
    </script>
    """,
    unsafe_allow_html=True,
)
