from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.config import settings
from core.logging import get_logger
from core.providers.base import AssuranceProvider

log = get_logger(__name__)

_BOOL_MAP = {"true": True, "false": False}


def _coerce(val):
    """Normalize Arize column values — converts 'true'/'false' strings to bool."""
    if isinstance(val, str):
        low = val.strip().lower()
        if low in _BOOL_MAP:
            return _BOOL_MAP[low]
    return val


def _to_ns(ts) -> int:
    """Convert a pandas Timestamp or datetime to nanoseconds epoch. Returns 0 on failure."""
    try:
        if ts is None:
            return 0
        import pandas as pd
        if isinstance(ts, pd.NaT.__class__) or pd.isnull(ts):
            return 0
        return int(ts.timestamp() * 1_000_000_000)
    except Exception:
        return 0


class ArizeProvider(AssuranceProvider):

    def is_available(self) -> bool:
        try:
            from arize import ArizeClient  # noqa: F401
            return bool(settings.arize_api_key and settings.arize_space_id)
        except ImportError:
            return False

    def fetch_spans(
        self,
        project_name: str,
        use_case: str,
        since_hours: int = 1,
    ) -> list[dict]:
        if not self.is_available():
            raise RuntimeError("ArizeProvider requires ARIZE_API_KEY and ARIZE_SPACE_ID in .env.")

        from arize import ArizeClient

        now        = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=since_hours)

        log.info(
            "Fetching spans from Arize | project=%s use_case=%s window=%sh",
            project_name, use_case, since_hours,
        )

        client = ArizeClient(api_key=settings.arize_api_key)
        df = client.spans.export_to_df(
            space_id=settings.arize_space_id,
            project_name=project_name,
            start_time=start_time,
            end_time=now,
        )

        if df.empty:
            log.info("No spans returned from Arize for %s/%s", project_name, use_case)
            return []

        name_col = next(
            (c for c in df.columns if "name" in c.lower() and "span" in c.lower()),
            next((c for c in df.columns if c == "name"), None),
        )

        # SDK-emitted chat turns: pair rai.guardrail.user_prompt + rai.guardrail.llm_response
        # by trace_id. Each pair becomes one synthesized support_chat span the evaluators
        # can consume. Legacy support_chat spans (e.g. our seeder) are kept as-is.
        if name_col and use_case == "support_chat":
            sdk_spans = self._pair_sdk_chat_turns(df, name_col)
            legacy_spans = self._extract_legacy_spans(
                df[df[name_col] == use_case], use_case,
            )
            spans = sdk_spans + legacy_spans
            log.info(
                "Fetched %d spans for %s in project '%s' (sdk_pairs=%d, legacy=%d)",
                len(spans), use_case, project_name, len(sdk_spans), len(legacy_spans),
            )
            return spans

        # Other use cases (loan_decision, …) keep strict name filtering.
        if name_col:
            df = df[df[name_col] == use_case]
        if df.empty:
            log.info("No '%s' spans in result set for %s", use_case, project_name)
            return []
        spans = self._extract_legacy_spans(df, use_case)
        log.info("Fetched %d '%s' spans from Arize project '%s'", len(spans), use_case, project_name)
        return spans

    def _pair_sdk_chat_turns(self, df, name_col: str) -> list[dict]:
        """Pair rai.guardrail.user_prompt + rai.guardrail.llm_response by trace_id."""
        trace_col = next((c for c in df.columns if "trace_id" in c.lower()), None)
        if not trace_col:
            return []

        prompts   = df[df[name_col] == "rai.guardrail.user_prompt"]
        responses = df[df[name_col] == "rai.guardrail.llm_response"]
        if prompts.empty or responses.empty:
            return []

        in_col = "attributes.input.value"
        sid_col = "attributes.session.id"

        pairs: list[dict] = []
        # Group both by trace_id. Each trace should yield one user→response pair.
        prompts_by_trace = {t: g.iloc[0] for t, g in prompts.groupby(trace_col)}
        resp_by_trace    = {t: g.iloc[0] for t, g in responses.groupby(trace_col)}

        for trace_id, prompt_row in prompts_by_trace.items():
            resp_row = resp_by_trace.get(trace_id)
            if resp_row is None:
                continue  # Blocked at user_prompt — no LLM response generated

            user_text = prompt_row.get(in_col) or prompt_row.get("input")
            resp_text = resp_row.get(in_col)   or resp_row.get("input")
            if not user_text or not resp_text:
                continue
            if str(user_text) in ("nan", "None", "") or str(resp_text) in ("nan", "None", ""):
                continue

            attrs = {
                "rai.user_message": str(user_text),
                "rai.response":     str(resp_text),
            }
            sid = prompt_row.get(sid_col)
            if sid and str(sid) not in ("nan", "None", ""):
                attrs["rai.session_id"] = str(sid)

            pairs.append({
                "name": "support_chat",
                "attributes": attrs,
                "start_time": _to_ns(prompt_row.get("start_time")),
                "end_time":   _to_ns(resp_row.get("end_time")),
                "status":     str(resp_row.get("status_code", "OK")),
            })
        return pairs

    def _extract_legacy_spans(self, df, use_case: str) -> list[dict]:
        """Extract legacy support_chat / loan_decision spans (our seeder format)."""
        spans = []
        for _, row in df.iterrows():
            attrs: dict = {}
            for col in df.columns:
                val = row[col]
                if val is None or str(val) in ("nan", "None", ""):
                    continue
                if col.startswith("attributes.rai."):
                    attrs[col.removeprefix("attributes.")] = _coerce(val)
                elif col.startswith("rai."):
                    attrs[col] = _coerce(val)

            for src_col, attr_key in [("input", "text.input"), ("output", "text.output")]:
                val = row.get(src_col)
                if val and str(val) not in ("nan", "None", ""):
                    attrs[attr_key] = str(val)

            spans.append({
                "name": use_case,
                "attributes": attrs,
                "start_time": _to_ns(row.get("start_time")),
                "end_time":   _to_ns(row.get("end_time")),
                "status": str(row.get("status_code", "OK")),
            })
        return spans
