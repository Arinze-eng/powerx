"""Regression tests for the cron JSON-serialization crash.

Channels inject ``RuntimeContextBlock`` dataclass instances into inbound
message metadata (``_runtime_context_blocks``). The cron tool copied that
metadata verbatim into a job's ``origin_metadata``, and persisting the store
or action.jsonl raised::

    TypeError: Object of type RuntimeContextBlock is not JSON serializable

every time a WebUI/Telegram turn with quote/session context scheduled a job.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_INPUT_META,
    RuntimeContextBlock,
    encode_runtime_context_blocks_for_json,
    runtime_context_blocks_from_metadata,
)


def _blocks() -> list[RuntimeContextBlock]:
    return [
        RuntimeContextBlock(
            source="webui_quote",
            content="[Runtime Context] selected excerpt [/Runtime Context]",
        )
    ]


# --------------------------------------------------------------------------- #
# encoder helper
# --------------------------------------------------------------------------- #
def test_encode_converts_blocks_to_plain_dicts() -> None:
    meta = {
        RUNTIME_CONTEXT_INPUT_META: _blocks(),
        "single_block": _blocks()[0],
        "webui": True,
        "other": {"nested": 1},
    }
    encoded = encode_runtime_context_blocks_for_json(meta)
    # Round-trips through json.dumps without raising.
    dumped = json.dumps(encoded, ensure_ascii=False)
    assert json.loads(dumped)[RUNTIME_CONTEXT_INPUT_META] == [
        {
            "source": "webui_quote",
            "content": "[Runtime Context] selected excerpt [/Runtime Context]",
        }
    ]
    # Non-block values pass through untouched.
    assert encoded["webui"] is True
    assert encoded["other"] == {"nested": 1}
    # Single block value is encoded too.
    assert isinstance(encoded["single_block"], dict)


def test_normalized_blocks_round_trip_from_encoded_form() -> None:
    original = _blocks()
    encoded = encode_runtime_context_blocks_for_json(
        {RUNTIME_CONTEXT_INPUT_META: original}
    )
    recovered = runtime_context_blocks_from_metadata(encoded)
    assert recovered == original


# --------------------------------------------------------------------------- #
# cron tool end-to-end
# --------------------------------------------------------------------------- #
def test_cron_add_with_runtime_context_blocks_succeeds_and_persists(
    tmp_path: Path,
) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    asyncio.run(service.start())  # marks the instance as store owner so writes persist
    tool = CronTool(service)
    ctx = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
        metadata={RUNTIME_CONTEXT_INPUT_META: _blocks(), "webui": True},
    )
    token = bind_request_context(ctx)
    try:
        out = asyncio.run(
            tool.execute(action="add", message="standup reminder", every_seconds=3600)
        )
    finally:
        reset_request_context(token)

    assert "Created job" in out
    job_id = out.split("id: ")[1].rstrip(")")

    # The store is flushed when the service stops; close it first.
    service.stop()

    # The durable store must be valid JSON with encoded (dict) blocks.
    store = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    jobs = store.get("jobs", store if isinstance(store, list) else [])
    job = next(j for j in jobs if j["id"] == job_id)
    origin_meta = job["payload"]["originMetadata"]
    assert origin_meta["webui"] is True
    blocks = origin_meta[RUNTIME_CONTEXT_INPUT_META]
    assert blocks == [
        {
            "source": "webui_quote",
            "content": "[Runtime Context] selected excerpt [/Runtime Context]",
        }
    ]

    # Action log lines are JSON-safe as well.
    action_log = tmp_path / "cron" / "action.jsonl"
    if action_log.exists():
        for line in action_log.read_text(encoding="utf-8").splitlines():
            json.loads(line)  # raises if any non-serializable object leaked

    # Reload path: from_store_dict accepts the encoded form unchanged.
    reloaded = CronJob.from_store_dict(job)
    assert runtime_context_blocks_from_metadata(reloaded.payload.origin_metadata) == _blocks()


def test_cron_add_without_request_context_still_works(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    out = asyncio.run(tool.execute(action="add", message="general ping", at=None, every_seconds=60))
    assert "Created job" in out
