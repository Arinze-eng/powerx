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
    raw_content = (last_user or {}).get("content")
    # ``content`` may be a plain string or an OpenAI-style multimodal array of
    # parts. Extract only the human-readable text here; every attachment part
    # (image or arbitrary file) is decoded to disk below and handed to the agent
    # as a local media path, never dumped inline into the prompt text.
    text_parts: list[str] = []
    if isinstance(raw_content, str):
        content = raw_content.strip()
    elif isinstance(raw_content, list):
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
        content = "\n".join(t for t in text_parts if t).strip()
    else:
        content = ""

    # --- attachments: OpenAI-style multimodal parts with base64 data URLs ----
    # Supports BOTH image parts (``{"type":"image_url","image_url":{"url":...}}``)
    # and arbitrary file parts (``{"type":"file","file":{"filename":...,
    # "file_data":"data:<mime>;base64,..."}}`` — also accepted under the
    # ``input_file`` alias). Any file type works (PDF, XLSX, DOCX, PPTX, ZIP,
    # APK, images, …): the bytes are written to the agent's media dir and passed
    # as a local path. The agent loop references non-image paths as
    # ``[Attachment: <path>]`` lines so the model reads them with its file tools
    # instead of receiving megabytes of base64 in the prompt.
    media_paths: list[str] = []
    from nanobot.config.paths import get_media_dir
    from nanobot.utils.media_decode import FileSizeExceededError, save_base64_data_url

    upload_error: str | None = None

    def _save_data_url(url: str, filename: str | None) -> None:
        nonlocal upload_error
        if not url.startswith("data:"):
            return
        try:
            saved = save_base64_data_url(url, get_media_dir("api"), filename=filename)
        except FileSizeExceededError as exc:
            upload_error = str(exc)
            return
        if saved:
            media_paths.append(saved)

    if isinstance(raw_content, list):
        for part in raw_content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("image_url", "input_image"):
                url_obj = part.get("image_url") or part.get("input_image")
                url = str(url_obj.get("url") if isinstance(url_obj, dict) else url_obj or "")
                _save_data_url(url, None)
            elif ptype in ("file", "input_file"):
                file_obj = part.get("file") or part.get("input_file") or {}
                if isinstance(file_obj, dict):
                    url = str(file_obj.get("file_data") or file_obj.get("url") or "")
                    filename = str(file_obj.get("filename") or "") or None
                else:
                    url = str(file_obj or "")
                    filename = None
                _save_data_url(url, filename)
    if upload_error:
        return _http_error(413, upload_error)
    if not content and not media_paths:
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

    key_id = record.get("id")
    # Billing identity comes straight from the key row (telegram_user_id kept
    # for backwards-compatible keys created before the column existed).
    user_id = str(record.get("agentx_user_id") or "").strip()
    if not user_id:
        return _http_error(409, "No AgentX account is linked to this API key yet")

    # --- run the turn -----------------------------------------------------
    want_stream = bool(body.get("stream"))
    text, meta = await api_bridge.run_turn(
        user_id=user_id,
        content=content or "(see attached file)",
        media_paths=media_paths,
        api_key_id=key_id,
        supabase_user_id=user_id,
        model=str(body.get("model") or ""),
        stream=want_stream,
    )
    err = {k: v for k, v in meta.items() if k == "error"}
    if err:
        message = str(err.get("error") or "agent error")
        status = 402 if "credit" in message.lower() else 502
        return _http_json_response(
            {"error": {"message": message, "type": "powerx_error", "code": status}},
            status=status,
        )


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
