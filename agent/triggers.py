"""Trigger endpoints — the ambient seam (pattern: ambient-expense-agent).

Cloud Scheduler POSTs /triggers/pulse | /triggers/nightly | /triggers/replenish | /triggers/health.
Each run is a fresh, recorded session driven through the shared Runner, so every
scheduled turn is inspectable in the ADK web UI and the ledger."""

import logging
import uuid

from fastapi import APIRouter, Request
from google.genai import types

log = logging.getLogger("stagenator.triggers")
router = APIRouter(prefix="/triggers", tags=["stagenator"])

SCHEDULER_USER = "scheduler"


async def _run(request: Request, message: str) -> dict:
    runner = request.app.state.runner
    session = await runner.session_service.create_session(
        app_name=request.app.state.agent_app_name,
        user_id=SCHEDULER_USER,
        session_id=f"run-{uuid.uuid4().hex[:12]}",
    )
    outputs = []
    async for event in runner.run_async(
        user_id=SCHEDULER_USER,
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=message)]
        ),
    ):
        if getattr(event, "output", None) is not None:
            outputs.append(event.output)
    return {"session": session.id, "final": outputs[-1] if outputs else None}


@router.post("/pulse")
async def pulse(request: Request) -> dict:
    return await _run(request, "pulse")


@router.post("/nightly")
async def nightly(request: Request) -> dict:
    return await _run(request, "nightly")


@router.post("/replenish")
async def replenish(request: Request) -> dict:
    return await _run(request, "replenish")


@router.post("/health")
async def health(request: Request) -> dict:
    """Full dependency health check — run daily by scheduler and at deploy."""
    raw = (await request.body()).decode("utf-8", "ignore").strip()
    msg = raw if raw.startswith("health") else "health:scheduled"
    return await _run(request, msg)
