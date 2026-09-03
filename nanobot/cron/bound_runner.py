"""Execution helpers for session-bound cron jobs."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from nanobot.agent.tools.cron import CronTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cron.session_delivery import origin_delivery_context
from nanobot.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META, CRON_TRIGGER_META
from nanobot.cron.types import CronJob
from nanobot.cron.webui_metadata import cron_proactive_delivery_metadata
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.agent.tools.registry import ToolRegistry


class BoundCronAgent(Protocol):
    tools: ToolRegistry

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        ...


class GeneralCronAgent(Protocol):
    """Minimal agent surface needed by the general (unbound) cron runner."""

    tools: ToolRegistry

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        sender_id: str | None = None,
        on_progress: Any = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Any:
        ...


class CronRunRecorder(Protocol):
    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        ...


def _cron_prompt_ref(prompt: str) -> dict[str, Any]:
    return {
        "id": "cron.agent_turn.reminder",
        "version": 1,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def _bound_session_delivery_context(
    job: CronJob,
    *,
    turn_seed: str,
    source_label: str | None,
) -> tuple[str, str, dict[str, Any]]:
    channel, chat_id, metadata = origin_delivery_context(job)

    if channel == "websocket":
        metadata["webui"] = True
        metadata.update(
            cron_proactive_delivery_metadata(
                "websocket",
                metadata,
                turn_seed=turn_seed,
                source_label=source_label,
            )
        )

    return channel, chat_id, metadata


async def run_bound_cron_job(
    job: CronJob,
    *,
    agent: BoundCronAgent,
    cron: CronRunRecorder,
) -> str | None:
    """Execute a session-bound cron job as a normal agent session turn."""
    session_key = job.payload.session_key
    if not session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    prompt = render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    run_id = f"{job.id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}"
    channel, chat_id, metadata = _bound_session_delivery_context(
        job,
        turn_seed=f"cron:{job.id}",
        source_label=job.name,
    )
    metadata[CRON_TRIGGER_META] = {
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_id,
        "prompt_ref": prompt_ref,
        "persist_content": (
            f"Scheduled cron job triggered: {job.name}\n\n{job.payload.message}"
        ),
    }
    metadata[CRON_DEFER_UNTIL_IDLE_META] = True
    run_record_base: dict[str, Any] = {
        "job_id": job.id,
        "job_name": job.name,
        "session_key": session_key,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": job.payload.message},
        "rendered_prompt": prompt,
    }

    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "queued",
        },
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    try:
        resp = await agent.submit_cron_turn(
            InboundMessage(
                channel=channel,
                sender_id="cron",
                chat_id=chat_id,
                content=prompt,
                metadata=metadata,
                session_key_override=session_key,
            )
        )
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        cron.write_run_record(
            run_id,
            {
                **run_record_base,
                "status": "error",
                "error": error_text,
            },
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = resp.content if resp else ""
    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "ok",
            "response": response,
        },
    )
    return response


def _general_cron_session_key(job: CronJob) -> str:
    """Stable general session key for an unbound (not chat-bound) cron job.

    Using a deterministic per-job session keeps the whole conversation in one
    place while isolating different general tasks from each other and from live
    user chats.
    """
    return f"cron-general:{job.id}"


async def run_general_cron_job(
    job: CronJob,
    *,
    agent: GeneralCronAgent,
    cron: CronRunRecorder,
    channel: str,
    chat_id: str,
) -> str | None:
    """Execute a general (unbound) cron job through the agent loop.

    Unlike :func:`run_bound_cron_job`, general jobs are not tied to a specific
    chat session. They run in a dedicated ``cron-general:<job_id>`` session so
    that ANY task the user scheduled — not just chat-bound reminders — actually
    executes. The response is returned to the caller (the gateway), which
    delivers it to ``channel``/``chat_id`` (a configured default target).
    """
    prompt = render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    run_id = f"{job.id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}"
    session_key = _general_cron_session_key(job)
    run_record_base: dict[str, Any] = {
        "job_id": job.id,
        "job_name": job.name,
        "session_key": session_key,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": job.payload.message},
        "rendered_prompt": prompt,
    }

    cron.write_run_record(run_id, {**run_record_base, "status": "queued"})

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    try:
        resp = await agent.process_direct(
            content=prompt,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            sender_id="cron",
        )
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        cron.write_run_record(
            run_id,
            {**run_record_base, "status": "error", "error": error_text},
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = str(getattr(resp, "content", "") or "").strip()
    cron.write_run_record(
        run_id,
        {**run_record_base, "status": "ok", "response": response},
    )
    return response or None
