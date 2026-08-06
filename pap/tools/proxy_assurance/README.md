# Mock Assurance Provider

Drop-in mock for the RAI Assurance v0.4 API contract.
FastAPI service on **port 3010**.

## Purpose

Lets you test the PAP dashboard's Assurance panel without a live upstream provider.
Five scenarios cover the full range of UI states: ok, no_data, provider_unavailable,
partial, and slow.

## Startup

Launched automatically by `start_all.bat` when `ASSURANCE_URL=http://127.0.0.1:3010`
(the default). To start it manually:

```bat
start_proxy.bat
```

Or with uvicorn directly:

```bat
uvicorn proxy_assurance:app --host 0.0.0.0 --port 3010 --reload
```

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/healthz` | Liveness + provider state |
| GET | `/v1/catalog?app_id=…` | Metric catalog (8 metrics) |
| GET | `/v1/metrics?app_id=…&ids=…&window=…` | Batch metric read |
| GET | `/v1/metrics/{metric_id}?app_id=…&window=…` | Single metric read |
| GET | `/v1/docs` | Swagger UI |
| GET | `/v1/redoc` | ReDoc |
| POST | `/v1/_test/scenario` | Switch scenario at runtime |
| GET | `/v1/_test/scenario` | Read current scenario |

## Scenarios

Switch at runtime — no restart needed:

```bash
curl -X POST "http://127.0.0.1:3010/v1/_test/scenario?scenario=no_data"
```

| Scenario | Behaviour |
|----------|-----------|
| `ok` (default) | All 8 metrics return realistic seed values |
| `no_data` | All metrics return `value=null`, `data_status=no_data` |
| `provider_unavailable` | All metrics fail; `/v1/healthz` reports `status=degraded` |
| `partial` | First 4 metrics ok, last 4 no_data; envelope `data_status=partial` |
| `slow` | 5-second sleep before every response (timeout / spinner testing) |

## Metric catalog

| metric_id | unit | 1h | 6h | 24h | 7d |
|-----------|------|-----|-----|-----|-----|
| hallucination_rate | % | 2.1 | 2.8 | 3.2 | 4.1 |
| groundedness_score | score | 0.891 | 0.882 | 0.874 | 0.851 |
| response_relevance | score | 0.844 | 0.831 | 0.819 | 0.803 |
| toxicity_judge_rate | % | 0.2 | 0.3 | 0.4 | 0.6 |
| prompt_injection_rate | % | 0.0 | 0.1 | 0.2 | 0.3 |
| pii_exposure_rate | % | 0.1 | 0.15 | 0.2 | 0.4 |
| retrieval_precision | % | 82.4 | 80.7 | 79.4 | 77.2 |
| context_utilisation | % | 67.5 | 65.2 | 63.8 | 61.0 |

## Replacing with a real provider

Set `ASSURANCE_URL` in the root `.env` to your real provider URL.
`start_all.bat` will skip launching this proxy automatically.
