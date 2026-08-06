Cognizant Neuro AI Trust Platform
=================================
Real-time governance, audit, and human oversight for LLM / agentic applications.
Wraps any instrumented app (reference app: MyBank) with policy evaluation on every
prompt, response, and tool call.

CORE FEATURES
-------------
Real-time guardrails
  - Every interaction scored against RAI policies before it proceeds.
  - Verdicts: Permissive / Normative / Coercive. Enforcement: Raise (block) / Redact / Audit-only.
  - Coverage across RAI pillars: Safety, Security, Privacy, Compliance, Agent-to-Agent.

Behavioral oversight (Sentinel)
  - Cross-turn pattern detection: structuring, collusion, conflict, persistence, scope excursion.
  - Emergent-risk alerts with auto-escalation to the Coordinator agent.

Human-in-the-loop
  - Escalation queues for human review; per-agent HITL mode (none / by design / human / agent).

Agent governance (Agent Registry)
  - Per-agent cards: purpose, tools, allowed/prohibited actions, max authority, kill-switch.
  - Live, asymmetric trust score computed from each agent's evaluation history.

Live dashboard
  - Cockpit: composite trust, RAI pillar dials, Trust Radar (behavioral-pattern alerts),
    open alerts, token + LLM cost, Sustainability (Green AI Index).
  - Eval Trail + Interactions lens (per-turn user -> agent -> tool provenance).
  - Live Agent Activity topology.

Policy administration (PAP)
  - Manage policies per dimension: thresholds, severity, remediation, incident routing.
  - Evaluation catalog, policy packs, session policy, provider assignment.

Multi-provider LLM
  - Provider selection: Gemini (Vertex) -> Anthropic -> Groq.
  - Cloud uses Gemini or Anthropic; local uses Groq or Anthropic.

Deployment
  - Single-container image (all services + nginx/supervisor) for Cloud Run.
  - Config-only DB snapshot ships in the image; audit history fills at runtime.

SERVICES  (started by start_all.bat, on localhost)
--------------------------------------------------
  3002  PAP               Admin + dashboard  (main entry point)
  3003  Policy Engine     Public API; all instrumented apps call this
  3001  Guardrail Service Content evaluation engine (PE-only)
  3012  Sentinel          Cross-turn behavioral audit
  3013  Coordinator       Escalation recommendations
  3010  Assurance         Quality metrics (mock in dev; Arize in prod)
  5000  MyBank            Reference banking assistant

QUICK START
-----------
  1. Copy each <service>/.env.example to <service>/.env and add your API keys.
  2. Run start_all.bat   (builds venvs, seeds the DB, launches all services).
  3. Open http://localhost:3002

GCP / CLOUD RUN NOTES  (read before deploying)
----------------------------------------------
URLs (all services behind one Cloud Run URL via nginx):
  /              -> MyBank (chat)            (the default route)
  /ui/           -> PAP admin UI
  /ui/dashboard  -> Live dashboard / Trust Radar
  /docs          -> PAP API docs

Deploy:  gcloud builds submit --config cloudbuild.yaml \
           --substitutions=_REGION=us-central1,_MODEL=gemini-2.5-flash,_MODEL_LOCATION=global .

LLM on cloud:  Gemini on Vertex (global endpoint, via langchain-google-genai) OR
  Anthropic. Set GOOGLE_CLOUD_PROJECT for Gemini; to force Claude instead, set
  ANTHROPIC_API_KEY and leave GOOGLE_CLOUD_PROJECT unset. Before deploying Gemini,
  confirm the model is entitled to your project with a direct call:
    curl -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
      https://aiplatform.googleapis.com/v1/projects/<PROJECT>/locations/global/publishers/google/models/<MODEL>:generateContent \
      -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}'
  200 = ship Gemini; 404 = not entitled, use the Anthropic fallback.

SINGLE INSTANCE ONLY:  the DB is SQLite on the instance's in-memory filesystem, so
  the image runs at --max-instances=1 (one source of truth). Do NOT raise it without
  first externalising the DB (Cloud SQL / mounted volume) — multiple instances would
  each get their own DB (split-brain: inconsistent dashboards).

DATA IS EPHEMERAL:  audit/metrics/HITL/trust-scores live in that in-memory SQLite and
  RESET to empty whenever the instance recycles (redeploy, maintenance, OOM). Fine for
  a time-boxed demo; use Cloud SQL if history must survive restarts.

RETENTION (keeps the in-memory DB from growing into the 4Gi RAM limit):
  PAP_RETENTION_METRICS_DAYS=1  trims metric snapshots hourly (no score impact)
  PAP_RETENTION_AUDIT_DAYS=7    audit ceiling; dormant on a <=7-day run (scores stable)
  Both set by cloudbuild.yaml (_RETENTION_DAYS / _AUDIT_RETENTION_DAYS).
