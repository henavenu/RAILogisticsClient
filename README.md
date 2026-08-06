# PharmaLogistics Trust Demo

Standalone, deployable build of the **RAI Trust Platform** with
**PharmaLogistics** — a pharmaceutical supply-chain assistant — as the default
governed application.

The point of the demo: every agent turn is evaluated in real time by a policy
stack (Policy Engine + Guardrail + Sentinel + Coordinator), and the governance
decision is visible in a live dashboard alongside the chat.

## Routes

| Path | What |
|---|---|
| `/` | **PharmaLogistics** chat app |
| `/mybank/` | MyBank chat app — same governance stack, second app_id |
| `/ui/dashboard` | Trust Radar / governance dashboard |
| `/ui` | PAP admin UI |
| `/docs` | PAP API docs |

Both apps register distinct identities (`pharmalogistics`, `mybank`) with one
shared PAP, so the dashboard shows cross-app governance in a single view.

## Deploying

See **[README_DEPLOY.md](README_DEPLOY.md)** — GitHub push and Cloud Run deploy,
step by step.

## Running locally

Two options.

**Container** — closest to what gets deployed, and the fastest way to see the
whole stack:

```bash
docker build -t pharmalogistics:local .
docker run --rm -p 8099:8080 -e PORT=8080 \
  -e GROQ_API_KEY=your_key_here \
  pharmalogistics:local
# → http://127.0.0.1:8099
```

Boot takes ~40–60s (seven services plus a SQLite seed). Swap `GROQ_API_KEY` for
`ANTHROPIC_API_KEY`, or set `LLM_DISABLED=true` for a stub model that exercises
governance without any LLM credential.

**Native processes** — better for editing code:

```bash
./start_all.sh                              # platform + MyBank on :5000
sdk/examples/pharma_logistics/run.sh        # PharmaLogistics on :5001
```

Each service reads its own `.env`, bootstrapped from the committed
`.env.example` on first run. Add your LLM key there. Stop with `./stop_all.sh`.

## Demo scenarios

`sdk/examples/pharma_logistics/` drives cold-chain, controlled-substance, and
restricted-destination scenarios. The deterministic ones need no LLM:

```bash
# Embargoed destination + denied party → COERCIVE, request blocked
curl -X POST localhost:8099/api/demo/sanctions_wire \
  -H 'Content-Type: application/json' -d '{"session_id":"demo1"}'
```

Demo login: `dana.reyes` / `1234`.

## Layout

```
sdk/examples/pharma_logistics/   PharmaLogistics app (LangGraph)
sdk/examples/mybank_clean/       MyBank app
pap/                             Policy Admin Platform + dashboard  :3002
policy-engine/                   Policy Engine                      :3003
guardrail-service/               Content evaluation                 :3001
trust-agents/sentinel/           Cross-turn audit                   :3012
trust-agents/coordinator/        Escalation                         :3013
sdk/, sdk-langgraph/             Instrumentation SDKs
docker/                          nginx + supervisord for the single container
```

Governance behaviour comes from `pap/config/pap.seed.db`, a committed
config-only snapshot materialized into `pap.db` on every boot.

## Upstream

Forked from the `Neuro-AI-Trust-MVP` repo (branch `Neuro-Trust-MVP`), which
remains the upstream for platform changes. The deltas carried here are listed at
the end of [README_DEPLOY.md](README_DEPLOY.md).
