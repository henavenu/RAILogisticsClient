"""
Example: RAI Trust SDK with OTEL telemetry

Demonstrates provider-agnostic OTLP tracing.  The same code runs against:
  - Local Jaeger (default, zero config)
  - Grafana Tempo
  - Arize (add endpoint + API key header)
  - Honeycomb
  - Datadog Agent

Install:
    pip install 'rai-trust-sdk[langchain,otel]'

Run Jaeger locally:
    docker run -p 4317:4317 -p 16686:16686 jaegertracing/all-in-one
    open http://localhost:16686
"""
import os

from rai_trust_sdk import TrustClient, GovernanceBlock
from rai_trust_sdk.langchain import RaiTrustCallbackHandler
from rai_trust_sdk.observability import setup_otel, GovernanceSpan
from rai_trust_sdk.types import ContentRole, EvalMode

# ── Configure OTEL ────────────────────────────────────────────────────────────

# Local Jaeger (default — no env vars needed)
tracer = setup_otel("finapp-agent")

# Arize example (uncomment to use):
# tracer = setup_otel(
#     "finapp-agent",
#     endpoint="https://otlp.arize.com:443",
#     headers={
#         "authorization": f"Bearer {os.environ['ARIZE_API_KEY']}",
#         "space_id": os.environ["ARIZE_SPACE_ID"],
#     },
#     insecure=False,
# )

# Honeycomb example:
# tracer = setup_otel(
#     "finapp-agent",
#     endpoint="https://api.honeycomb.io",
#     protocol="http",
#     headers={"x-honeycomb-team": os.environ["HONEYCOMB_API_KEY"]},
#     insecure=False,
# )

# ── Trust client ──────────────────────────────────────────────────────────────

client = TrustClient(
    base_url=os.environ.get("TRUST_PE_URL", "http://localhost:3000"),
    app_id="finapp",
)

# ── Manual evaluation with GovernanceSpan ─────────────────────────────────────

content = "Wire $50,000 to account 8812-XX"

with GovernanceSpan(tracer, role=ContentRole.USER_PROMPT, app_id="finapp") as gs:
    try:
        result = client.evaluate(
            content=content,
            role=ContentRole.USER_PROMPT,
            eval_mode=EvalMode.SYNC,
        )
        gs.record_result(result)
        print(f"Verdict: {result.verdict.value}")
    except GovernanceBlock as block:
        gs.record_result(block.result)
        print(f"Blocked: {block}")

# ── LangChain with automatic tracing via callback ─────────────────────────────
# The callback handler emits one evaluation per event.
# Wrap each evaluate call with GovernanceSpan in on_result for full trace coverage.

def on_result_with_trace(result, role):
    """Emit a governance span for each evaluation triggered by the callback."""
    with GovernanceSpan(tracer, role=role, app_id="finapp") as gs:
        gs.record_result(result)
    print(f"[Trace] role={role.value} verdict={result.verdict.value}")

handler = RaiTrustCallbackHandler(
    client=client,
    eval_mode=EvalMode.SYNC,
    on_result=on_result_with_trace,
)
