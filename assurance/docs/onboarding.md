# RAI Trust Platform — Onboarding

Welcome! You've been added as a collaborator on
**https://github.com/henavenu/RAI-Trust-Platform**

## 1. Accept the invite

You should have received an email from GitHub. Click "View invitation"
and accept. Or visit:
https://github.com/henavenu/RAI-Trust-Platform/invitations

## 2. Clone the repo

```bash
git clone https://github.com/henavenu/RAI-Trust-Platform.git
cd RAI-Trust-Platform
```

## 3. Set up Python environment

Requires **Python 3.11+** (3.12 tested).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 4. Configure your `.env`

`.env` is **gitignored** (contains secrets) — you need to create your own
at the repo root. Minimal contents:

```bash
# --- Arize ---
ARIZE_API_KEY=<your-arize-api-key>
ARIZE_SPACE_ID=<your-arize-space-id>

# --- LLM (one of these is required for evals) ---
ANTHROPIC_API_KEY=<your-anthropic-key>
OPENAI_API_KEY=<your-openai-key>

# --- Provider config ---
ASSURANCE_PROVIDER=arize         # or "mock" for offline
JUDGE_MODEL=claude-haiku-4-5     # used by Phoenix LLM-as-judge
EVAL_FAIRNESS=true               # set false to skip live LLM evals
```

Ping Hena if you need help getting Arize credentials.

## 5. Verify the setup

```bash
# Run the test suite (uses mock provider, no live API calls)
pytest

# Run a smoke test against Arize (live)
python scripts/smoke.py

# Start the API
uvicorn api.main:app --reload --port 3010
# → http://localhost:3010/v1/docs

# Start the Streamlit dashboard (separate terminal)
streamlit run dashboard/app.py
```

## 6. Optional — React dashboard

```bash
cd dashboard-react
npm install
npm run dev
# → http://localhost:3000
```

## 7. Project structure (high-level)

```
api/         FastAPI service — /metrics/* endpoints (4 pillars)
core/        Config, models, Arize provider SPI
metrics/     Tier-2 statistical metrics (fairness, safety, reliability)
evals/       PII regex + Phoenix LLM-as-judge evaluators
rai_client/  LangGraph apps (loan_decision, support_chat) — emits traces
scripts/     One-shot runners (smoke, daily_report, *_eval)
tests/       pytest suite
docs/        integration_guide.md has the full walkthrough
```

## Questions?

Ping Hena. The architecture diagram in the root README and
`docs/integration_guide.md` are the best starting points for context.
