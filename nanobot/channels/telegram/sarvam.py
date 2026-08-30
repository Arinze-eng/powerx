from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import requests

INDUS_ORIGIN = "https://indus.sarvam.ai"
CHAT_API = "https://chat.sarvamclaw.sarvam.ai"


class SarvamAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class SarvamResult:
    text: str
    file_urls: tuple[str, ...] = ()


class SarvamClient:
    """Indus conversation client. Secrets are read only from environment variables."""

    def __init__(self, *, bot_token: str, cache_path: str | None = None) -> None:
        self.bot_token = bot_token
        self.cache_path = Path(cache_path or os.path.expanduser("~/.sarvam_jwt_cache.json"))
        self._lock = asyncio.Lock()

    @staticmethod
    def _jwt_expiry(token: str) -> int | None:
        try:
            payload = token.split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            return int(json.loads(base64.urlsafe_b64decode(payload).decode())["exp"])
        except Exception:
            return None

    @classmethod
    def _valid(cls, token: str | None, margin: int = 90) -> bool:
        exp = cls._jwt_expiry(token or "")
        return bool(exp and exp > time.time() + margin)

    def _load_cached(self) -> str | None:
        try:
            data = json.loads(self.cache_path.read_text())
            token = data.get("token")
            return token if self._valid(token) else None
        except Exception:
            return None

    def _save_cached(self, token: str) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"token": token, "exp": self._jwt_expiry(token)}))
            self.cache_path.chmod(0o600)
        except OSError:
            pass

    def _mint(self) -> str:
        sid = os.environ.get("SARVAM_SID", "").strip()
        csrf = os.environ.get("SARVAM_CSRF", "").strip()
        org = os.environ.get("SARVAM_ORG", "").strip()
        workspace = os.environ.get("SARVAM_WS", "").strip()
        cookie_name = os.environ.get(
            "SARVAM_CSRF_COOKIE_NAME",
            "csrf_token_47e7312b4098ea2074fa42ed3e882b46089ff81e1306152464548a6023a49a22",
        ).strip()
        if not all((sid, csrf, org, workspace)):
            raise SarvamAuthError("Sarvam session credentials are not configured")
        response = requests.post(
            f"{INDUS_ORIGIN}/api/auth/token",
            json={"org_id": org, "workspace_id": workspace},
            headers={
                "content-type": "application/json",
                "origin": INDUS_ORIGIN,
                "referer": f"{INDUS_ORIGIN}/indus",
                "cookie": f"sarvam_identity_session={sid}; {cookie_name}={csrf}",
                "user-agent": "Mozilla/5.0",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise SarvamAuthError(f"Sarvam JWT mint failed ({response.status_code})")
        token = response.json().get("token")
        if not token:
            raise SarvamAuthError("Sarvam JWT mint returned no token")
        self._save_cached(token)
        return token

    def token(self, *, force: bool = False) -> str:
        if not force:
            cached = self._load_cached()
            if cached:
                return cached
        return self._mint()

    def _request(self, method: str, path: str, *, token: str, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update({"authorization": f"Bearer {token}", "origin": INDUS_ORIGIN})
        return requests.request(method, f"{CHAT_API}{path}", headers=headers, **kwargs)

    def _send_sync(self, conversation_id: str, parts: list[dict[str, Any]]) -> SarvamResult:
        logger = __import__("logging").getLogger("nanobot.telegram.sarvam")
        logger.info("Sarvam request started (conversation configured, parts=%d)", len(parts))
        for attempt in range(2):
            token = self.token(force=attempt == 1)
            first = self._request(
                "POST", f"/v1/conversations/{conversation_id}/messages", token=token,
                json={"role": "user", "parts": parts},
                headers={"content-type": "application/json", "accept": "*/*"}, timeout=120,
            )
            if first.status_code == 401 and attempt == 0:
                logger.info("Sarvam JWT rejected; refreshing and retrying")
                continue
            if first.status_code == 401:
                raise SarvamAuthError("Sarvam authentication expired; refresh failed")
            first.raise_for_status()
            message_id = first.json().get("id")
            if not message_id:
                raise RuntimeError("Sarvam returned no message id")
            stream = self._request(
                "POST", "/v1/responses", token=token,
                json={"id": conversation_id, "message": {"id": message_id, "role": "user", "parts": parts}},
                headers={"content-type": "application/json", "accept": "text/event-stream"},
                stream=True, timeout=300,
            )
            if stream.status_code == 401 and attempt == 0:
                continue
            if stream.status_code == 401:
                raise SarvamAuthError("Sarvam authentication expired during response")
            stream.raise_for_status()
            text: list[str] = []
            file_urls: list[str] = []
            for raw in stream.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "text-delta":
                    text.append(event.get("delta", ""))
                elif event.get("type") in {"text", "finish-step"}:
                    text.append(event.get("text", ""))
                self._collect_file_urls(event, file_urls)
            result = "".join(text).strip()
            for match in re.findall(r"https?://[^\s)<>\"]+", result):
                self._collect_file_urls({"url": match}, file_urls)
            urls = tuple(dict.fromkeys(file_urls))
            logger.info("Sarvam response completed (characters=%d, files=%d)", len(result), len(urls))
            return SarvamResult(text=result, file_urls=urls)
        raise RuntimeError("Sarvam request failed")

    @staticmethod
    def _collect_file_urls(value: Any, output: list[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_l = str(key).lower()
                if key_l in {"url", "file_url", "download_url", "artifact_url", "output_url", "downloadurl"} and isinstance(item, str):
                    parsed = urlparse(item)
                    if parsed.scheme in {"http", "https"} and item not in output:
                        output.append(item.rstrip(".,"))
                SarvamClient._collect_file_urls(item, output)
        elif isinstance(value, list):
            for item in value:
                SarvamClient._collect_file_urls(item, output)

    async def send_result(self, conversation_id: str, parts: list[dict[str, Any]]) -> SarvamResult:
        async with self._lock:
            return await asyncio.to_thread(self._send_sync, conversation_id, parts)

    async def send(self, conversation_id: str, parts: list[dict[str, Any]]) -> str:
        return (await self.send_result(conversation_id, parts)).text

    async def telegram_file_url(self, file_id: str) -> str:
        response = await self._telegram_get_file(file_id)
        file_path = response.get("file_path")
        if not file_path:
            raise RuntimeError("Telegram returned no file path")
        return f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"

    async def _telegram_get_file(self, file_id: str) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            response = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getFile",
                params={"file_id": file_id}, timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError("Telegram file lookup failed")
            return payload.get("result", {})
        return await asyncio.to_thread(fetch)


def telegram_file_ref(message: Any) -> tuple[str, str] | None:
    if getattr(message, "document", None):
        obj = message.document
        return obj.file_id, getattr(obj, "file_name", "document") or "document"
    if getattr(message, "video", None):
        obj = message.video
        return obj.file_id, "video"
    if getattr(message, "audio", None):
        obj = message.audio
        return obj.file_id, getattr(obj, "file_name", "audio") or "audio"
    if getattr(message, "voice", None):
        return message.voice.file_id, "voice.ogg"
    if getattr(message, "animation", None):
        return message.animation.file_id, "animation"
    if getattr(message, "photo", None):
        obj = message.photo[-1]
        return obj.file_id, "photo.jpg"
    return None
