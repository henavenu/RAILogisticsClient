"""
LLM Client — httpx-based, OpenAI-compatible endpoint.

Works with Ollama (local container) or any OpenAI-compatible API.
Swap LLM_BASE_URL env var to switch providers without code changes.

  Local Ollama:   LLM_BASE_URL=http://localhost:11434/v1
  Claude via API: LLM_BASE_URL=https://api.anthropic.com/v1  (+ anthropic SDK)
  OpenAI:         LLM_BASE_URL=https://api.openai.com/v1
"""
from __future__ import annotations

import json
import logging
import os
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
_MODEL    = os.getenv("LLM_MODEL",    "mistral:7b-instruct-q4_K_M")
_TIMEOUT  = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
_API_KEY  = os.getenv("LLM_API_KEY", "ollama")   # Ollama ignores this; required field


async def complete(
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    """
    Single-shot completion. Blocks until the full response is received.
    Returns the assistant message content string.
    Raises httpx.HTTPError on network / HTTP failure.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            json={
                "model":       _MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens":  max_tokens,
                "stream":      False,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def stream_complete(
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> AsyncGenerator[str, None]:
    """
    Streaming completion. Yields text tokens as they arrive from the LLM.
    Caller is responsible for assembling the full text if needed.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            json={
                "model":       _MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens":  max_tokens,
                "stream":      True,
            },
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    token = chunk["choices"][0]["delta"].get("content", "")
                    if token:
                        yield token
                except (json.JSONDecodeError, KeyError):
                    continue


async def is_available() -> bool:
    """Quick health check — returns True if the LLM endpoint responds."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{_BASE_URL}/models")
            return r.status_code < 500
    except Exception:
        return False
