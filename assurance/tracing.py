"""
Central tracing module. Call setup_tracing() once at process start.
Registers the global OTLP tracer provider pointing at Arize and
optionally instruments LangChain/LangGraph + the active LLM SDK.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def setup_tracing(
    project_name: str = "rai-assurance",
    *,
    instrument_langchain: bool = True,
    instrument_openai: bool = False,
    instrument_anthropic: bool = False,
) -> None:
    space_id = os.environ.get("ARIZE_SPACE_ID", "")
    api_key = os.environ.get("ARIZE_API_KEY", "")

    if not space_id or not api_key:
        raise EnvironmentError(
            "ARIZE_SPACE_ID and ARIZE_API_KEY must be set. "
            "Copy .env.example → .env and fill in the values."
        )

    from arize.otel import register

    # register() sets the global TracerProvider and ships spans via OTLP to Arize.
    register(
        space_id=space_id,
        api_key=api_key,
        project_name=project_name,
    )

    if instrument_langchain:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument()

    if instrument_openai:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()

    if instrument_anthropic:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        AnthropicInstrumentor().instrument()
