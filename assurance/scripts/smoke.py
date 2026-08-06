"""
Arize tracing smoke test — makes a single LLM call and ships the trace to Arize.

Usage:
    python scripts/smoke.py                  # auto-detects key
    python scripts/smoke.py --provider openai
    python scripts/smoke.py --provider anthropic

Confirm the span arrived: open Arize → project 'rai-assurance-smoke'.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"ERROR: missing environment variable(s): {', '.join(missing)}")
        sys.exit(1)


def _call_openai() -> str:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Say hi in one word"}],
        max_tokens=10,
    )
    return resp.choices[0].message.content or ""


def _call_anthropic() -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": "Say hi in one word"}],
    )
    return msg.content[0].text  # type: ignore[union-attr]


def main() -> None:
    parser = argparse.ArgumentParser(description="Arize tracing smoke test")
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic", "auto"],
        default="auto",
        help="LLM provider to use (default: auto-detect from env)",
    )
    args = parser.parse_args()

    provider = args.provider
    if provider == "auto":
        if os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        else:
            print("ERROR: set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env")
            sys.exit(1)

    _require_env("ARIZE_SPACE_ID", "ARIZE_API_KEY")

    from tracing import setup_tracing

    setup_tracing(
        project_name="rai-assurance-smoke",
        instrument_langchain=False,
        instrument_openai=(provider == "openai"),
        instrument_anthropic=(provider == "anthropic"),
    )

    from opentelemetry import trace

    tracer = trace.get_tracer("smoke")

    print(f"Using provider: {provider}")
    with tracer.start_as_current_span("smoke-test") as span:
        span.set_attribute("app_id", "smoke")
        span.set_attribute("provider", provider)

        if provider == "openai":
            _require_env("OPENAI_API_KEY")
            response_text = _call_openai()
        else:
            _require_env("ANTHROPIC_API_KEY")
            response_text = _call_anthropic()

        span.set_attribute("llm.response", response_text)

    print(f"LLM response: {response_text!r}")

    tp = trace.get_tracer_provider()
    if hasattr(tp, "force_flush"):
        print("Flushing spans to Arize...", end=" ", flush=True)
        tp.force_flush(timeout_millis=15_000)
        print("done.")

    print("\nSmoke test PASSED.")
    print("→ Open Arize and verify a span named 'smoke-test' exists in project 'rai-assurance-smoke'.")


if __name__ == "__main__":
    main()
