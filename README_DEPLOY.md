# PharmaLogistics Trust Demo — Git + Cloud Run Runbook

Standalone deploy of the RAI Trust Platform with **PharmaLogistics** as the
default app. Produces one public URL your team opens to run the demo.

What the deployed URL serves:

| Path | What |
|---|---|
| `/` | **PharmaLogistics** chat app |
| `/mybank/` | MyBank chat app (same governance stack) |
| `/ui/dashboard` | Trust Radar / governance dashboard |
| `/ui` | PAP admin UI |
| `/docs` | PAP API docs |

Everything runs in **one container**: nginx + PAP + Policy Engine + Guardrail +
Sentinel + Coordinator + trust-agents + Mock Assurance + both apps. Cloud Run
exposes a single port; nginx fans out to the services on loopback.

---

## Part 0 — Prerequisites (one time, on your Mac)

`git` and `docker` are already installed. `gcloud` is **not**.

```bash
# 1. Install the Google Cloud CLI
brew install --cask google-cloud-sdk

# 2. Confirm
gcloud version
```

Log in — run these two yourself, they open a browser:

```bash
gcloud auth login
gcloud auth application-default login
```

> In Claude Code you can run these inline by prefixing with `!`, e.g. `! gcloud auth login`

Optional, for step 1: `brew install gh && gh auth login`

---

## Part 1 — Push to GitHub

The repo is already initialized and committed locally at
`/Users/981061/PharmaLogistics-Demo`. It has **no remote yet** — that is the only
thing standing between you and a pushed repo.

### 1a. Create the empty repo under the `sharadcognizant` org

With the `gh` CLI:

```bash
cd /Users/981061/PharmaLogistics-Demo
gh repo create sharadcognizant/pharma-logistics-trust-demo \
  --private \
  --source=. \
  --remote=origin \
  --push
```

That single command creates it, wires `origin`, and pushes. **If it succeeds, skip
to Part 2.**

Or via the web UI: https://github.com/organizations/sharadcognizant/repositories/new
— name `pharma-logistics-trust-demo`, **private**, and do **not** initialize with a
README (the local repo already has one).

### 1b. Push (only if you used the web UI)

```bash
cd /Users/981061/PharmaLogistics-Demo
git remote add origin https://github.com/sharadcognizant/pharma-logistics-trust-demo.git
git branch -M main
git push -u origin main
```

### 1c. Verify no secrets went up

```bash
git ls-files | grep -E '\.env$' ; echo "exit=$? (1 = good, no .env tracked)"
git grep -nE 'gsk_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9]' -- . | head
```

Both should come back empty. `.env` files are gitignored and were never copied
into this repo; only `.env.example` templates are tracked.

---

## Part 2 — One-time GCP setup

```bash
export PROJECT_ID=cog01kmzvxwkcp4jrwtxtgttgsnh0
export REGION=us-east4
export SERVICE=pharmalogistics
export AR_REPO=neuro-ai-trust

gcloud config set project "$PROJECT_ID"
```

### 2a. Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com
```

### 2b. Artifact Registry repo

Reuses the existing `neuro-ai-trust` repo. Create it only if missing:

```bash
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" \
  || gcloud artifacts repositories create "$AR_REPO" \
       --repository-format=docker --location="$REGION" \
       --description="RAI Trust Platform images"
```

### 2c. Let the runtime service account call Vertex AI

The app picks its LLM from whichever credential is present, and
`GOOGLE_CLOUD_PROJECT` (set by `cloudbuild.yaml`) selects **Gemini on Vertex AI**.
Vertex authenticates as the service account — **no API key is needed in the
cloud**, which is why nothing secret appears in this repo.

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

<details>
<summary>Alternative: use a Groq or Anthropic key instead of Vertex</summary>

Only if Vertex is unavailable to you. Put the key in Secret Manager — never in
the repo or the image:

```bash
printf '%s' 'YOUR_KEY_HERE' | gcloud secrets create groq-api-key --data-file=-
gcloud secrets add-iam-policy-binding groq-api-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

Then add to the deploy in 3b:
`--set-secrets=GROQ_API_KEY=groq-api-key:latest` **and** remove
`GOOGLE_CLOUD_PROJECT` from `--set-env-vars` — its presence outranks Groq in the
provider chain.
</details>

---

## Part 3 — Build and deploy

### 3a. Recommended: one command, builds in the cloud

```bash
cd /Users/981061/PharmaLogistics-Demo
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)
```

This builds the image, pushes it to Artifact Registry, and deploys the Cloud Run
service — all settings (public access, 8Gi, warm instance) are in
`cloudbuild.yaml`. Expect **8–15 minutes** on the first run; later builds reuse
cached layers.

### 3b. Alternative: deploy from local source

Slower on upload (~180 MB) but does not need `cloudbuild.yaml`:

```bash
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 8Gi --cpu 4 \
  --min-instances 1 --max-instances 3 \
  --concurrency 40 --timeout 300 \
  --cpu-boost --no-cpu-throttling \
  --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=gemini-2.5-flash
```

### 3c. Get the URL

```bash
gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)'
```

Share that link. `--allow-unauthenticated` means **anyone with the URL can open
it** — no Google sign-in. See "Locking it down" below.

### 3d. Smoke-test before you send it out

```bash
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
for p in / /mybank/ /ui/dashboard; do
  printf '%-16s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$p")"
done
```

All three should be `200`. Then open `$URL` and send one chat message — that
confirms the LLM credential and the Policy Engine round-trip, which a status code
alone does not.

---

## Part 4 — Operating it

**Logs**
```bash
gcloud run services logs tail "$SERVICE" --region "$REGION"
```

**Redeploy after a code change**
```bash
git add -A && git commit -m "…" && git push
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD)
```

**Cut cost between demos.** The warm instance is the main charge — 4 vCPU / 8 GiB
held 24/7. Scale to zero when idle:
```bash
gcloud run services update "$SERVICE" --region "$REGION" --min-instances 0
```
Set it back to `1` an hour before a demo. At `min-instances 0` the first request
after idle waits ~40–60s for seven services to boot, and may time out — always
warm it up yourself before sending the link around.

**Locking it down** (replaces "anyone with the link"):
```bash
gcloud run services update "$SERVICE" --region "$REGION" --no-allow-unauthenticated
gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" \
  --member="user:teammate@cognizant.com" --role="roles/run.invoker"
```
Each teammate then needs to be signed in to that Google account.

---

## Known constraints

**State is per-instance and ephemeral.** `pap.db` lives on the container
filesystem, re-seeded from `pap/config/pap.seed.db` on every cold start. So:
audit history and HITL queues reset whenever Cloud Run recycles the instance, and
two instances do not share state. `--max-instances 3` plus `--concurrency 40`
keeps a demo audience on one instance in practice, but it is not guaranteed — if
a viewer sees an empty dashboard mid-demo, they landed on a fresh instance.
Making this durable means moving PAP to Cloud SQL, which this demo does not do.

**Both apps share one `/api/*` namespace.** PharmaLogistics owns the origin root;
MyBank is served under `/mybank/` and its UI was changed to use *relative* API
paths so its calls stay inside the prefix. If you re-sync
`sdk/examples/mybank_clean/ui/index.html` from the upstream MVP repo, re-apply
that change — otherwise MyBank's chat will silently drive PharmaLogistics. The
reasoning is documented in `docker/nginx.conf.tmpl` and at the top of that HTML file.

**Demo policy seed.** Governance behaviour comes from `pap/config/pap.seed.db`.
Re-generate it with `tools/make_seed_db.py` after policy changes, and commit it —
the deployed service has no other source of policy config.

---

## Relationship to the MVP repo

Copied from `Neuro-AI-Trust-MVP` (branch `Neuro-Trust-MVP`) — that repo is
untouched and remains the upstream. Deltas here:

- `docker/supervisord.conf` — added the `pharmalogistics` program; per-app
  `TRUST_APP_ID` / port / callback so both apps have distinct governance identities
- `docker/nginx.conf.tmpl` — PharmaLogistics at `/`, MyBank at `/mybank/`
- `docker/entrypoint.sh` — per-app identity moved out to supervisord; startup banner
- `Dockerfile` — installs `sdk/examples/pharma_logistics/requirements.txt`
- `sdk/examples/mybank_clean/ui/index.html` — absolute → relative API paths
- `cloudbuild.yaml` — service `pharmalogistics`, public access, parameterized

Excluded from the copy: `.git/`, all `venv/`, all `.env` (secrets),
`__pycache__/`, `.launch-logs/`, logs, and the runtime `pap.db`.
