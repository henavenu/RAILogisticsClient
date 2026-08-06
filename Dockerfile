# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# RAI Trust Platform — single-container image (demo / time-boxed deploy).
# Runs ALL services (Guardrail, PAP, Policy Engine, Sentinel, Coordinator,
# MyBank) behind an nginx reverse proxy on Cloud Run's $PORT. See
# docs/DEPLOY_GCP_CloudRun.md for the rationale, caveats, and deploy steps.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# NOTE: do NOT set PIP_NO_CACHE_DIR here — it would defeat the BuildKit pip cache
# mounts below (--mount=type=cache,target=/root/.cache/pip), which is what makes
# rebuilds fast (downloads are reused instead of re-fetched).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# System deps: nginx (proxy), supervisor (process mgr), bash/curl (entrypoint).
RUN apt-get update && apt-get install -y --no-install-recommends \
      nginx supervisor bash curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps: 1) third-party (PyPI) ───────────────────────────────────────
# Copy each service's requirements.txt to its real path first (a separate, cached
# layer that only re-runs when a requirements file changes — not on every code
# edit). These files list PyPI deps ONLY; the in-repo packages are installed in
# step 2. We also add the LangChain provider SDKs the services select from
# (gemini->anthropic->azure->groq) so the chain works regardless of which key is
# supplied. Gemini uses langchain-google-genai (google-genai SDK, >=4.0) — it
# addresses the Vertex 'global' endpoint correctly, unlike langchain-google-vertexai.
COPY requirements.txt ./requirements.txt
COPY trust-agents/sentinel/requirements.txt ./trust-agents/sentinel/requirements.txt
COPY trust-agents/coordinator/requirements.txt ./trust-agents/coordinator/requirements.txt
COPY sdk/examples/mybank_clean/requirements.txt ./sdk/examples/mybank_clean/requirements.txt
COPY sdk/examples/pharma_logistics/requirements.txt ./sdk/examples/pharma_logistics/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
      -r requirements.txt \
      -r trust-agents/sentinel/requirements.txt \
      -r trust-agents/coordinator/requirements.txt \
      -r sdk/examples/mybank_clean/requirements.txt \
      -r sdk/examples/pharma_logistics/requirements.txt \
      "langchain-google-genai>=4.0" langchain-anthropic langchain-openai langchain-groq

# ── Python deps: 2) local in-repo packages ───────────────────────────────────
# Each ships a pyproject.toml, so we install them like any package — by path, no
# editable mode, no PYTHONPATH. Order matters: rai-sdk-langgraph depends on
# rai-trust-sdk, so ./sdk precedes ./sdk-langgraph (pip resolves the dependency to
# the local copy already in the build set, never reaching out to PyPI). The heavy
# OTel exporter extras (grpcio/protobuf) are deliberately NOT installed.
COPY privacy-proxy ./privacy-proxy
COPY sdk ./sdk
COPY sdk-langgraph ./sdk-langgraph
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ./privacy-proxy ./sdk ./sdk-langgraph

# ── App code + container config ──────────────────────────────────────────────
COPY . .
# Normalise CRLF→LF on the shell scripts/configs (they may be checked out with
# Windows line endings, which the container's bash/nginx would reject), then
# install the container config and make the scripts executable.
RUN sed -i 's/\r$//' docker/entrypoint.sh docker/wait_for.sh \
       docker/supervisord.conf docker/nginx.conf.tmpl \
 && cp docker/nginx.conf.tmpl /etc/nginx/nginx.conf.tmpl \
 && cp docker/supervisord.conf /etc/supervisor/conf.d/rai.conf \
 && chmod +x docker/entrypoint.sh docker/wait_for.sh

# Cloud Run provides $PORT at runtime (default 8080 for local docker run).
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
