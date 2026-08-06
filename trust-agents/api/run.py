"""
POST /v1/trust/run/{agent_type}   — trigger an immediate agent run (sync, returns JSON)
GET  /v1/trust/stream/{agent_type} — trigger a run and stream LLM reasoning via SSE
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from core import compliance_agent, tracker_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/trust", tags=["Trust Intelligence"])

AgentType = Literal["compliance", "tracker"]

_AGENTS = {
    "compliance": compliance_agent,
    "tracker":    tracker_agent,
}

# One lock per agent — prevents overlapping runs during demo
_locks: dict[str, asyncio.Lock] = {
    "compliance": asyncio.Lock(),
    "tracker":    asyncio.Lock(),
}


@router.post("/run/{agent_type}")
async def run_agent(agent_type: AgentType):
    """
    Trigger an immediate synchronous agent run.
    Returns the complete finding JSON when the LLM finishes.
    Use GET /v1/trust/stream/{agent_type} for live streaming output.
    """
    lock = _locks[agent_type]
    if lock.locked():
        return JSONResponse(
            status_code=409,
            content={"detail": f"{agent_type} agent is already running — try again shortly"},
        )

    async with lock:
        module = _AGENTS[agent_type]
        result = await module.run_sync()
        return result


@router.get("/stream/{agent_type}")
async def stream_agent(agent_type: AgentType):
    """
    Trigger an agent run and stream LLM reasoning tokens via Server-Sent Events.
    The dashboard connects here for the live 'Run Now' demo.

    SSE event format:
      data: {"type": "token",  "content": "<text>"}
      data: {"type": "done",   "content": ""}
      data: {"type": "error",  "content": "<message>"}
    """
    lock = _locks[agent_type]
    if lock.locked():
        async def _busy():
            yield f'data: {json.dumps({"type":"error","content":f"{agent_type} agent already running"})}\n\n'
        return StreamingResponse(_busy(), media_type="text/event-stream")

    module = _AGENTS[agent_type]
    generator = await module.run_stream()

    async def _sse():
        async with lock:
            try:
                async for token in generator:
                    payload = json.dumps({"type": "token", "content": token})
                    yield f"data: {payload}\n\n"
                yield f'data: {json.dumps({"type": "done", "content": ""})}\n\n'
            except Exception as exc:
                logger.error(f"[StreamAgent] {agent_type} stream error: {exc}")
                yield f'data: {json.dumps({"type": "error", "content": str(exc)})}\n\n'

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":  "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )
