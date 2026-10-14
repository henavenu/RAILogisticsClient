"""
Local gateway for the beta Trust platform — mirrors the GKE ingress
(deploy/helm/rai-trust/templates/ingress.yaml) on one port, so the dashboard runs
"behind a proxy" exactly as it does on GKE.

Why: served directly on :3004, the dashboard's Human Review tab calls a relative
`/v1/hitl`, which only the ingress routes to PAP — locally it 404s and the queue looks
empty. Through this gateway every relative call lands where the ingress would send it.

    <venv>/bin/python trust_gateway.py      # http://127.0.0.1:3080/ui/dashboard

Local only: on GKE/GCP the ingress already does this, so the gateway isn't needed there.
It also adds a deep link the beta dashboard lacks: /ui/dashboard?goto=hitl opens the
Human Review tab (used by the client apps' "Open Human Review" link).
"""
from __future__ import annotations

import os
import re

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

PAP = os.getenv("GW_PAP", "http://127.0.0.1:3002")
DASH = os.getenv("GW_DASHBOARD", "http://127.0.0.1:3004")
AGENTS = {"sentinel": os.getenv("GW_SENTINEL", "http://127.0.0.1:3012"),
          "coordinator": os.getenv("GW_COORDINATOR", "http://127.0.0.1:3013"),
          "trust": os.getenv("GW_TRUST_AGENTS", "http://127.0.0.1:3005")}

# rai-ops ingress: the Dashboard's read-only /v1 paths (more specific than /v1 → PAP)
_DASH_PREFIX = re.compile(r"^/(?:ui|static|v1/(?:metrics|cogs|alerts|analytics|sre|trust-score|sessions|"
                          r"reliability|agent-topology|governance/alerts|privacy)|internal/alerts)(?:/|$)")
_AGENTS = re.compile(r"^/agents/(sentinel|coordinator|trust)(?:/|$)(.*)$")
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None), follow_redirects=False)


def _target(path: str, method: str) -> str:
    m = _AGENTS.match(path)
    if m:
        return AGENTS[m.group(1)] + "/" + m.group(2)
    if path.startswith("/v1/agent-cards"):                 # method split (server-snippet)
        return (DASH if method in ("GET", "HEAD") else PAP) + path
    if _DASH_PREFIX.match(path):
        return DASH + path
    return PAP + path                                      # /v1, /internal, /docs → PAP


@app.get("/")
def root():
    return RedirectResponse("/ui/dashboard")


# ?goto=hitl → open the Human Review tab once the dashboard has loaded.
_OPEN_HITL = (b"<script>window.addEventListener('load',function(){setTimeout(function(){"
              b"var t=document.getElementById('hitl-tab-link');if(t)t.click();"
              b"try{history.replaceState({},'',location.pathname)}catch(e){}},1800)});</script>")


@app.get("/ui/dashboard")
async def dashboard(request: Request):
    if request.query_params.get("goto") != "hitl":
        return await proxy("ui/dashboard", request)
    r = await client.get(DASH + "/ui/dashboard", headers={"accept-encoding": "identity"})
    body = r.content.replace(b"</body>", _OPEN_HITL + b"</body>", 1)
    return Response(body, status_code=r.status_code, media_type="text/html",
                    headers={"cache-control": "no-store"})


@app.api_route("/{full:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(full: str, request: Request):
    url = _target("/" + full, request.method)
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    upstream = client.build_request(request.method, url, headers=headers, content=await request.body())
    resp = await client.send(upstream, stream=True)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out_headers,
                             background=None if resp.is_closed else _Closer(resp))


class _Closer:
    def __init__(self, resp):
        self.resp = resp

    async def __call__(self):
        await self.resp.aclose()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("GW_PORT", "3080")), log_level="warning")
