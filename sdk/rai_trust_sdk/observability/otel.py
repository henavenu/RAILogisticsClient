"""
Provider-agnostic OTEL telemetry for the RAI Trust SDK.

This module speaks the OTLP wire protocol only — no vendor SDK is hard-coded.
Any OTLP-compatible backend will work without code changes:

  Backend              Endpoint example
  ───────              ────────────────
  Local Jaeger         http://localhost:4317  (gRPC, default)
  Grafana Tempo        http://tempo:4317
  Honeycomb            https://api.honeycomb.io:443  + API key header
  Arize                https://otlp.arize.com:443    + API key header
  Datadog              https://trace.agent.datadoghq.com:4317
  AWS X-Ray (ADOT)     http://otel-collector:4317

To switch backends, change ``endpoint`` (and optionally ``headers``).
No SDK code changes required.

Quick start
───────────
::

    from rai_trust_sdk.observability import setup_otel

    tracer = setup_otel("my-finapp-agent")           # → local Jaeger default
    tracer = setup_otel(                              # → Arize
        "my-finapp-agent",
        endpoint="https://otlp.arize.com:443",
        headers={"authorization": "Bearer <ARIZE_API_KEY>"},
        insecure=False,
    )

Environment variables (standard OTEL spec)
──────────────────────────────────────────
  OTEL_EXPORTER_OTLP_ENDPOINT   collector URL
  OTEL_EXPORTER_OTLP_HEADERS    key=val,key2=val2  (parsed automatically)
  OTEL_SERVICE_NAME             overrides service_name parameter
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_OTLP_DEFAULT_GRPC = "http://localhost:4317"
_OTLP_DEFAULT_HTTP = "http://localhost:4318"


# ── JsonlFileSpanExporter ─────────────────────────────────────────────────────

class JsonlFileSpanExporter:
    """
    Write completed OTel spans to a newline-delimited JSON file.

    Each line is one JSON object with keys:
      name, trace_id, span_id, parent_span_id,
      start_time_us, end_time_us, duration_ms,
      status, attributes, events

    Usage — via environment variable (no code changes in app):
      OTEL_FILE_EXPORT_PATH=traces.jsonl python smoke_test.py

    Usage — explicit (alongside OTLP exporter):
      setup_otel("myapp", endpoint="http://...", file_export_path="traces.jsonl")
    """

    def __init__(self, file_path: str) -> None:
        self._path = file_path
        self._file = open(file_path, "a", encoding="utf-8")
        logger.info("[RAI OTEL] JsonlFileSpanExporter → %s", file_path)

    # SpanExporter interface
    def export(self, spans: Any) -> Any:
        try:
            from opentelemetry.sdk.trace.export import SpanExportResult
            for span in spans:
                line = json.dumps(self._span_to_dict(span), default=str)
                self._file.write(line + "\n")
            self._file.flush()
            return SpanExportResult.SUCCESS
        except Exception as exc:
            logger.warning("[RAI OTEL] FileExporter write error: %s", exc)
            try:
                from opentelemetry.sdk.trace.export import SpanExportResult
                return SpanExportResult.FAILURE
            except ImportError:
                return 1  # fallback non-zero → failure

    def shutdown(self) -> None:
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            self._file.flush()
            return True
        except Exception:
            return False

    @staticmethod
    def _span_to_dict(span: Any) -> dict:
        ctx = span.context
        parent = span.parent

        attrs = dict(span.attributes) if span.attributes else {}

        events = []
        for ev in (span.events or []):
            events.append({
                "name":       ev.name,
                "timestamp":  ev.timestamp,
                "attributes": dict(ev.attributes) if ev.attributes else {},
            })

        start_us = span.start_time // 1_000 if span.start_time else None
        end_us   = span.end_time   // 1_000 if span.end_time   else None
        dur_ms   = (
            round((span.end_time - span.start_time) / 1_000_000, 2)
            if span.start_time and span.end_time else None
        )

        return {
            "name":           span.name,
            "trace_id":       f"{ctx.trace_id:032x}" if ctx else None,
            "span_id":        f"{ctx.span_id:016x}"  if ctx else None,
            "parent_span_id": f"{parent.span_id:016x}" if parent else None,
            "start_time_us":  start_us,
            "end_time_us":    end_us,
            "duration_ms":    dur_ms,
            "status":         span.status.status_code.name if span.status else None,
            "status_desc":    span.status.description if span.status else None,
            "attributes":     attrs,
            "events":         events,
        }


# ── Public API ────────────────────────────────────────────────────────────────

def setup_otel(
    service_name: str,
    *,
    endpoint: Optional[str] = None,
    protocol: Optional[str] = None,
    headers: Optional[dict] = None,
    insecure: Optional[bool] = None,
    file_export_path: Optional[str] = None,
) -> Any:
    """
    Bootstrap OTEL tracing with an OTLP exporter and return a ``Tracer``.

    Parameters
    ──────────
    service_name : str
        Service name shown in the trace backend UI.
    endpoint : str, optional
        OTLP collector endpoint.
        Defaults to ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, then
        ``http://localhost:4317`` (gRPC) or ``http://localhost:4318`` (HTTP).
    protocol : str
        ``"grpc"`` (default) or ``"http"``.
        HTTP exporter appends ``/v1/traces`` automatically.
    headers : dict, optional
        Extra HTTP headers sent with every export batch.
        Useful for API-key auth (Arize, Honeycomb, …).
        Can also be set via ``OTEL_EXPORTER_OTLP_HEADERS=key=val,key2=val2``.
    insecure : bool
        Skip TLS certificate verification.  ``True`` (default) for local
        collectors; set to ``False`` for production HTTPS endpoints.

    Returns
    ───────
    ``opentelemetry.trace.Tracer``

    Raises
    ──────
    ImportError
        When ``opentelemetry-sdk`` is not installed.
    ValueError
        When ``protocol`` is not ``"grpc"`` or ``"http"``.

    Example — Arize
    ───────────────
    ::

        tracer = setup_otel(
            "finapp-agent",
            endpoint="https://otlp.arize.com:443",
            headers={"authorization": "Bearer <KEY>", "space_id": "<SPACE>"},
            insecure=False,
        )

    Example — local Jaeger (no config needed)
    ─────────────────────────────────────────
    ::

        tracer = setup_otel("finapp-agent")
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.resources import SERVICE_NAME
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError as exc:
        raise ImportError(
            "opentelemetry-sdk is required. "
            "Install with: pip install 'rai-trust-sdk[otel]'"
        ) from exc

    # File export path: explicit arg → env var
    resolved_file_path = file_export_path or os.environ.get("OTEL_FILE_EXPORT_PATH")

    # Resolved endpoint: explicit arg → env var → protocol-specific default.
    # When only file export is requested (no endpoint), skip the OTLP exporter.
    resolved_endpoint = (
        endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )

    # Protocol: explicit arg → env → "grpc" default
    resolved_protocol = (
        protocol
        or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower().strip()
    )
    if resolved_protocol not in ("grpc", "http"):
        raise ValueError(f"protocol must be 'grpc' or 'http', got {resolved_protocol!r}")

    # Headers: merge env headers first, then caller-supplied (caller wins on conflicts).
    resolved_headers = {**(_parse_headers_env() or {}), **(headers or {})} or None

    # TLS: explicit arg → OTEL_EXPORTER_OTLP_INSECURE env → True (local default)
    if insecure is None:
        _env_insecure = os.environ.get("OTEL_EXPORTER_OTLP_INSECURE", "").lower()
        insecure = _env_insecure != "false"

    resolved_name = os.environ.get("OTEL_SERVICE_NAME") or service_name

    resource = Resource(attributes={SERVICE_NAME: resolved_name})
    provider = TracerProvider(resource=resource)

    # ── OTLP exporter (Phoenix / Arize / Jaeger / any OTLP backend) ──────────
    if resolved_endpoint:
        _ep = (
            resolved_endpoint
            if resolved_endpoint
            else (_OTLP_DEFAULT_GRPC if resolved_protocol == "grpc" else _OTLP_DEFAULT_HTTP)
        )
        otlp_exporter = (
            _make_grpc_exporter(_ep, resolved_headers, insecure)
            if resolved_protocol == "grpc"
            else _make_http_exporter(_ep, resolved_headers, insecure)
        )
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info(
            "[RAI OTEL] service=%r endpoint=%r protocol=%s insecure=%s",
            resolved_name, _ep, resolved_protocol, insecure,
        )

    # ── File exporter (local JSONL — always SimpleSpanProcessor for immediacy) ─
    if resolved_file_path:
        file_exporter = JsonlFileSpanExporter(resolved_file_path)
        provider.add_span_processor(SimpleSpanProcessor(file_exporter))
        logger.info("[RAI OTEL] service=%r file_export=%r", resolved_name, resolved_file_path)

    if not resolved_endpoint and not resolved_file_path:
        logger.warning(
            "[RAI OTEL] No endpoint or file path configured — spans will be dropped. "
            "Set OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_FILE_EXPORT_PATH."
        )

    trace.set_tracer_provider(provider)
    return trace.get_tracer(resolved_name)


# ── GovernanceSpan ─────────────────────────────────────────────────────────────

class GovernanceSpan:
    """
    Context manager that creates an OpenInference GUARDRAIL span for a
    governance evaluation.

    For most use cases, prefer the auto-instrumentation built into
    ``TrustClient.evaluate()`` — it creates this span automatically as a child
    of the active LangGraph / LangChain span without any host-app wiring.

    Use ``GovernanceSpan`` directly only when you need to place a span at a
    location not covered by the callback/guard layer (e.g. a custom async
    evaluation pathway or a non-LangChain framework).

    Attributes follow the OpenInference semantic conventions so spans are
    visible in Arize / Phoenix alongside framework-generated spans and are
    consumable by Arize evaluators:

      openinference.span.kind  = "GUARDRAIL"
      input.value              = content evaluated (set on __enter__)
      input.mime_type          = "text/plain" | "application/json"
      output.value             = JSON verdict payload (set in record_result)
      output.mime_type         = "application/json"
      session.id               = session_id (when present)
      metadata                 = JSON context blob (app_id, role, verdict …)

    Usage
    ─────
    ::

        with GovernanceSpan(
            tracer,
            content="Transfer $500 to RU account",
            role=ContentRole.USER_PROMPT,
            app_id="finapp",
            session_id="sess-abc",
        ) as gs:
            result = client.evaluate(content, role=ContentRole.USER_PROMPT,
                                     raise_on_block=False)
            gs.record_result(result)
    """

    # Resolved attribute names — use openinference-semantic-conventions constants
    # when available, fall back to string literals (same values, guaranteed stable).
    try:
        from openinference.semconv.trace import (
            SpanAttributes              as _OIAttrs,
            OpenInferenceSpanKindValues as _OIKind,
        )
        _A_KIND     = _OIAttrs.OPENINFERENCE_SPAN_KIND
        _A_IN_VAL   = _OIAttrs.INPUT_VALUE
        _A_IN_MIME  = _OIAttrs.INPUT_MIME_TYPE
        _A_OUT_VAL  = _OIAttrs.OUTPUT_VALUE
        _A_OUT_MIME = _OIAttrs.OUTPUT_MIME_TYPE
        _A_SESSION  = _OIAttrs.SESSION_ID
        _A_META     = _OIAttrs.METADATA
        _V_GUARDRAIL = _OIKind.GUARDRAIL.value
    except ImportError:
        _A_KIND     = "openinference.span.kind"
        _A_IN_VAL   = "input.value"
        _A_IN_MIME  = "input.mime_type"
        _A_OUT_VAL  = "output.value"
        _A_OUT_MIME = "output.mime_type"
        _A_SESSION  = "session.id"
        _A_META     = "metadata"
        _V_GUARDRAIL = "GUARDRAIL"

    _ROLE_MIME = {
        "user_prompt":    "text/plain",
        "llm_response":   "text/plain",
        "tool_call":      "application/json",
        "agent_to_agent": "application/json",
    }

    def __init__(
        self,
        tracer: Any,
        content: str,
        role: "ContentRole",
        app_id: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        self._tracer      = tracer
        self._content     = content
        self._role        = role
        self._app_id      = app_id
        self._session_id  = session_id
        self._request_id  = request_id
        self._span: Any   = None
        self._ctx_manager: Any = None

    def __enter__(self) -> "GovernanceSpan":
        import json as _json
        # Use start_as_current_span so the span is:
        #   1. automatically parented to the active root span (e.g. rai.governance.turn)
        #   2. set as the active context so TrustClient._guardrail_span() nests under it
        self._ctx_manager = self._tracer.start_as_current_span(
            f"rai.guardrail.{self._role.value}"
        )
        self._span = self._ctx_manager.__enter__()
        # ── OpenInference input attributes ────────────────────────────────────
        self._span.set_attribute(self._A_KIND,    self._V_GUARDRAIL)
        self._span.set_attribute(self._A_IN_VAL,  self._content)
        self._span.set_attribute(
            self._A_IN_MIME,
            self._ROLE_MIME.get(self._role.value, "text/plain"),
        )
        if self._session_id:
            self._span.set_attribute(self._A_SESSION, self._session_id)
        # Seed metadata — enriched on record_result()
        self._span.set_attribute(
            self._A_META,
            _json.dumps({
                "app_id":     self._app_id,
                "role":       self._role.value,
                "request_id": self._request_id,
            }),
        )
        return self

    def record_result(self, result: "GovernanceResult") -> None:
        """
        Attach verdict detail to the open span.

        Call this after ``client.evaluate()`` returns.  The span is ended
        automatically on ``__exit__`` — this method only enriches it.
        """
        if self._span is None:
            return

        import json as _json
        from opentelemetry.trace import StatusCode

        # ── output.value — structured for Arize evaluators ───────────────────
        output_doc = {
            "verdict":            result.verdict.value,
            "violations":         result.violations,
            "remediations":       result.remediations,
            "failed_evaluations": [
                fe.model_dump(exclude_none=True)
                for fe in result.failed_evaluations
            ],
        }
        self._span.set_attribute(self._A_OUT_VAL,  _json.dumps(output_doc))
        self._span.set_attribute(self._A_OUT_MIME, "application/json")

        # ── metadata — full context in Arize span detail ─────────────────────
        meta: dict = {
            "app_id":     self._app_id,
            "role":       self._role.value,
            "request_id": result.request_id,
            "verdict":    result.verdict.value,
        }
        if result.policies:
            meta["policies"] = [p.model_dump(exclude_none=True) for p in result.policies]
        if result.pack_trace:
            meta["pack_trace"] = result.pack_trace
        if result.session_summary:
            meta["session_summary"] = result.session_summary.model_dump()
        self._span.set_attribute(self._A_META, _json.dumps(meta))

        # ── OTel status ───────────────────────────────────────────────────────
        # Compare on .value string to avoid a cross-subpackage import.
        if result.verdict.value == "COERCIVE":
            self._span.set_status(
                StatusCode.ERROR,
                f"Governance block: {'; '.join(result.violations) or 'policy violation'}",
            )
        else:
            self._span.set_status(StatusCode.OK)

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Any,
    ) -> bool:
        if self._span is not None:
            if exc_type is not None:
                try:
                    from opentelemetry.trace import StatusCode
                    self._span.set_status(StatusCode.ERROR, str(exc_val))
                except ImportError:
                    pass
            # Delegate to the context manager so the span is ended AND removed
            # from the active context stack (mirrors start_as_current_span behaviour).
            ctx_mgr = getattr(self, "_ctx_manager", None)
            if ctx_mgr is not None:
                ctx_mgr.__exit__(exc_type, exc_val, exc_tb)
            else:
                self._span.end()
        return False  # Never suppress exceptions


# ── Private helpers ───────────────────────────────────────────────────────────

def _make_grpc_exporter(endpoint: str, headers: Optional[dict], insecure: bool) -> Any:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        raise ImportError(
            "opentelemetry-exporter-otlp-proto-grpc is required. "
            "Install with: pip install 'rai-trust-sdk[otel]'"
        ) from exc
    return OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers or {},
        insecure=insecure,
    )


def _make_http_exporter(endpoint: str, headers: Optional[dict], insecure: bool) -> Any:
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        raise ImportError(
            "opentelemetry-exporter-otlp-proto-http is required. "
            "Install with: pip install 'rai-trust-sdk[otel]'"
        ) from exc
    # HTTP exporter appends /v1/traces automatically — pass base URL only
    return OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers or {},
    )


def _parse_headers_env() -> Optional[dict]:
    """Parse ``OTEL_EXPORTER_OTLP_HEADERS=k=v,k2=v2`` into a dict."""
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if not raw:
        return None
    headers: dict = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers or None
