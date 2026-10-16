# VitaLink on Cloud Run

How to containerise VitaLink Distribution (`pharmalogistics`) and deploy it to Cloud Run
against the NeuroTrust platform, following *RAI-Trust-SDK-CloudRun-Deployment-Guide*
(October 2026). The container was built and run locally against the beta platform:
SDK `governance ACTIVE eval=SYNC enforce=RAISE`, `scenario_check.py` ALL PASS (12/12).

## What's in the repo for this

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, installs `requirements.txt` + the SDK from `sdk-dist/wheelhouse`, deletes `rai.env` / `.env` / `trust_gateway.py`, runs as a non-root user, listens on `$PORT` (8080). |
| `.dockerignore` | Keeps `.env`, `rai.env`, `venv/`, logs and `docs/` out of the image. |
| `cloudbuild.yaml` | Build → push to Artifact Registry → `gcloud run deploy` (one instance). |
| `cloudrun.env.example` | Every environment variable the service needs. |

`server.py` now listens on `PORT` when set (Cloud Run), else `MYBANK_PORT` / 5001 (local).

## Before you deploy: confirm with the platform team

1. **IAP.** The platform URL sits behind Google Identity-Aware Proxy: an anonymous request
   gets a 302 to Google sign-in. The SDK (0.1.7) sends no IAP
   token. Ask how a client Cloud Run service should authenticate (IAP access for its
   service account plus an OIDC token, an internal route that bypasses IAP, or an SDK
   build that adds the token).
2. **App registration.** Who registers `pharmalogistics` and issues `RAI_APP_KEY` on the
   cloud platform. `onboard_trust.py --pap <url> --pe <url>` does it locally, but it also
   calls Policy Engine `/internal/packs` and `/internal/app-stacks/…`, which the ingress
   may not expose. Same for `onboard_supply_chain.py`.
4. **Human Review deep link.** `?goto=hitl` was added by the local gateway; check the
   cloud dashboard supports it, or the link opens the dashboard home.

## Steps

### 1. Local checks
```bash
venv/bin/python scenario_check.py          # local app on :5001 — ALL PASS
docker build -t vitalink .
docker run -p 8080:8080 --env-file cloudrun.env vitalink   # cloudrun.env from the example
venv/bin/python scenario_check.py http://127.0.0.1:8080
```
To run the container against the local beta platform, use `http://host.docker.internal:3002/3003/3012/3013` for the URLs.

### 2. Commit and push
Branch `main` of https://github.com/henavenu/RAILogisticsClient.

### 3. GCP setup (once)
```bash
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create vitalink --repository-format=docker --location=us-east1
printf '%s' '<RAI_APP_KEY>' | gcloud secrets create vitalink-rai-app-key --data-file=-
```
If the LLM is Gemini on Vertex, grant the Cloud Run service account `roles/aiplatform.user`.

### 4. Build, push and deploy
```bash
gcloud builds submit --config cloudbuild.yaml --substitutions=_REGION=us-east1,_REPO=vitalink,_SERVICE=vitalink .
```
Then set the variables once (they persist across redeploys):
```bash
gcloud run services update vitalink --region us-east1 \
  --env-vars-file cloudrun.yaml \
  --set-secrets RAI_APP_KEY=vitalink-rai-app-key:latest
```
(`cloudrun.yaml` is `cloudrun.env.example` in `KEY: value` form, without the secret.)

### 5. Verify
- Logs show `PE … connected`, `PAP … connected`, `Ready -- governance ACTIVE eval=SYNC enforce=RAISE`.
- `venv/bin/python scenario_check.py https://<service-url>` passes.
- Evaluations appear on the platform dashboard (`/ui/`) under `pharmalogistics`.

## Deployed (15 Oct 2026)

- Project `cb9252819a-raigov-gc` (project ID `cog01kmzvxwkcp4jrwtxtgttgsnh0`), service `vitalink` in `us-east1`:
  https://vitalink-82745682889.us-east1.run.app
- Created with Cloud Run **Connect repository → Cloud Build** (GitHub `main`, build type
  Dockerfile `/Dockerfile`). Every push to `main` rebuilds and redeploys; the service keeps
  its environment variables.
- Required at startup: `TRUST_PE_URL`, `PAP_URL`, `SENTINEL_URL`. Without one the container
  exits ("Refusing to start") and Cloud Run reports it failed to listen on port 8080.
  Sentinel and Coordinator are at `/agents/sentinel` and `/agents/coordinator` on the
  platform URL.
- Until IAP lets the service through and `RAI_APP_KEY` is set, the SDK starts
  `Ready (DEGRADED) -- observe-only eval=ASYNC enforce=NONE`: the app runs but nothing is
  enforced.

## Rules for this app

- **One instance** (`--max-instances=1`, `--min-instances=1`). Sessions, Human Review holds
  and order counts live in memory; a second instance or a cold start loses them.
- **Trailing slashes.** `RAI_PE_URL` / `RAI_PAP_URL` end with `/pe/` and `/pap/` (SDK).
  `TRUST_PE_URL` / `PAP_URL` have none, because the app appends `/v1/…` itself.
- **`MYBANK_CALLBACK_URL`** is the service's own URL; PAP calls `/api/hitl/resolve` on approval.
- **Privacy proxy.** `RAI_PRIVACY_PROXY=regex` (the image has no spaCy model for presidio).
- **Groq model.** If you use Groq, set `GROQ_MODEL` (the code's default `llama-3.1-8b-instant`
  no longer exists on Groq; `.env` uses `openai/gpt-oss-20b`).
