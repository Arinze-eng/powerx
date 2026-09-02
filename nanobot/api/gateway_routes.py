"""OpenAI-compatible ``/v1/*`` routes served by the WebUI gateway HTTP router.

Render (and any single-process deployment) runs ``nanobot gateway`` only — the
standalone ``nanobot serve`` API process is not running there. These handlers
mirror the aiohttp routes in :mod:`nanobot.api.server` using the same response
helpers as the Mini App bridge, so ``https://<host>/v1/chat/completions`` works
on the gateway origin with user ``px_...`` keys and full credit billing.

The websockets HTTP layer never exposes request bodies, so POST payloads arrive
either as a query parameter (``?payload=<json>&token=...``) or via the
Authorization header for token-only GET-style calls. Clients that must send
large prompts can use ``curl --data-urlencode`` style GET requests.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from websockets.http11 import Request as WsRequest

from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from nanobot.webui.http_utils import (
    http_response as _http_response,
)
from nanobot.webui.http_utils import (
    parse_query as _parse_query,
)


def _chatcmpl_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def _completion(content: str, model: str, *, stream: bool = False) -> dict[str, Any]:
    prompt = max(1, len(content) // 4) if content else 1
    completion = max(1, len(content) // 4) if content else 1
    return {
        "id": _chatcmpl_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


async def dispatch_v1_routes(request: WsRequest, got: str) -> Any | None:
    """Handle /v1/* OpenAI-compatible routes on the gateway. Returns Response or None."""
    from nanobot.api.api_bridge import api_bridge
    from nanobot.api.api_keys import ApiKeyStore, hash_api_key, looks_like_api_key
    from nanobot.channels.telegram.api_platform import render_docs

    method = getattr(request, "method", "GET")
    model_name = _model_name()

    # GET /v1/models — no auth required (mirrors server.py which allows it too,
    # but keep parity: models list is public info).
    if got == "/v1/models":
        return _http_json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model_name,
                        "object": "model",
                        "created": 0,
                        "owned_by": "powerx",
                    }
                ],
            }
        )

    # GET /v1/api-docs — plain-text integration docs (no auth).
    if got == "/v1/api-docs":
        return _http_response(
            render_docs().encode("utf-8"),
            status=200,
            content_type="text/plain; charset=utf-8",
        )

    if got != "/v1/chat/completions":
        return _http_error(404, "Unknown endpoint")
    method = getattr(request, "method", None)
    if method is not None and method.upper() != "POST":
        return _http_error(405, "Use POST")

    # --- authentication --------------------------------------------------
    auth = _header(request, "authorization") or ""
    supplied = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    query = _parse_query(request.path)
    supplied = supplied or (_first(query, "token") or "").strip()
    payload_raw = (_first(query, "payload") or "").strip()

    body: dict[str, Any] = {}
    if payload_raw:
        try:
            parsed = json.loads(payload_raw)
            if isinstance(parsed, dict):
                body = parsed
        except (ValueError, TypeError):
            return _http_error(400, "Invalid JSON in payload parameter")
    if not body:
        return _http_error(400, "Missing request body (send ?payload=<json>)")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _http_error(400, "'messages' must be a non-empty array")
    last_user = next(
        (m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"),
        None,
    )
    content = str((last_user or {}).get("content") or "").strip()
    if not content:
        return _http_error(400, "No user message content")

    store = ApiKeyStore()
    if not store.enabled:
        return _http_error(503, "API platform is not configured on this server")
    if not supplied or not looks_like_api_key(supplied):
        return _http_error(401, "Provide your px_... key via Authorization: Bearer")
    record = await store.find_active_by_hash(hash_api_key(supplied))
    if not record:
        return _http_error(401, "Invalid API key")

    if not api_bridge.ready:
        return _http_error(503, "Gateway agent is starting up, retry shortly")

    session_user = str(record.get("agentx_user_id") or "")
    key_id = record.get("id")

    # --- run the turn -----------------------------------------------------
    text, err = await api_bridge.run_turn(
        user_id=session_user,
        content=content,
        api_key_id=key_id,
    )
    if err:
        message = str(err.get("error") or "agent error")
        status = 402 if "credit" in message.lower() else 502
        return _http_json_response(
            {"error": {"message": message, "type": "powerx_error", "code": status}},
            status=status,
        )

    want_stream = bool(body.get("stream"))
    completion = _completion(text, str(body.get("model") or model_name))
    if want_stream:
        chunk = {
            "id": completion["id"],
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": completion["model"],
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ],
        }
        final = {
            "id": completion["id"],
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": completion["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        sse = (
            f"data: {json.dumps(chunk)}\n\n"
            f"data: {json.dumps(final)}\n\n"
            "data: [DONE]\n\n"
        )
        return _http_response(
            sse.encode("utf-8"),
            status=200,
            content_type="text/event-stream; charset=utf-8",
            extra_headers=[("Cache-Control", "no-cache")],
        )
    return _http_json_response(completion)


def _model_name() -> str:
    import os

    return os.getenv("NANOBOT_API_MODEL_NAME", "").strip() or "powerx-agent"


def _header(request: WsRequest, name: str) -> str:
    try:
        return request.headers.get(name, "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None
