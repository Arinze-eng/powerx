from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.http11 import Response

from nanobot import dbq_admin, supabase_admin
from nanobot.agent.tools.vps_backend import (
    VPSExecutionBackend,
    normalize_vps_private_key,
    validate_vps_fingerprint,
    validate_vps_host,
    validate_vps_username,
    validate_vps_workspace,
)
from nanobot.config.loader import load_config, resolve_env_refs, save_config
from nanobot.execution_env import apply_render_execution_env
from nanobot.webui.http_utils import http_error, http_json_response, http_response

_LOCK = threading.RLock()
_MAX_API_BASE = 2048
_MAX_API_KEY = 4096
_MAX_MODEL_ID = 512


def _data_dir() -> Path:
    configured = os.getenv("NANOBOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("HOME", ".")) / ".nanobot"


def _users_path() -> Path:
    return _data_dir() / "admin_users.json"


def _config_path() -> Path:
    return _data_dir() / "config.json"


def record_telegram_user(
    *,
    sender_id: str,
    chat_id: str,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    is_bot: bool = False,
) -> None:
    """Record non-secret Telegram identity/activity metadata for the admin view."""
    path = _users_path()
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(payload, dict):
                payload = {}
        except (OSError, ValueError):
            payload = {}
        key = str(sender_id)
        old = payload.get(key) if isinstance(payload.get(key), dict) else {}
        payload[key] = {
            "sender_id": key,
            "chat_id": str(chat_id),
            "username": username or old.get("username"),
            "first_name": first_name or old.get("first_name"),
            "last_name": last_name or old.get("last_name"),
            "is_bot": bool(is_bot),
            "first_seen": old.get("first_seen", now),
            "last_seen": now,
            "message_count": int(old.get("message_count", 0)) + 1,
        }
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return


def list_users() -> list[dict[str, Any]]:
    with _LOCK:
        try:
            payload = json.loads(_users_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    rows = [row for row in payload.values() if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("last_seen", "")), reverse=True)
    return rows


def _admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "").strip()


def _authorized(request: Any) -> bool:
    expected = _admin_password()
    if bool(getattr(request, "_nanobot_admin_authenticated", False)):
        return bool(expected)
    header = str(getattr(request, "headers", {}).get("Authorization", "") or "")
    if not expected or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return False
    import hmac
    # The admin identity is the secret password; browsers and reverse proxies
    # vary in how they populate the Basic Auth username field. Accepting any
    # username keeps the dashboard password-driven without weakening the
    # constant-time password check.
    return hmac.compare_digest(password, expected)


def _auth_error() -> Response:
    return http_response(
        b"Authentication required",
        status=401,
        extra_headers=[("WWW-Authenticate", 'Basic realm="Nanobot Admin"')],
    )


def _payload(request: Any) -> dict[str, Any]:
    mutation_payload = getattr(request, "_nanobot_webui_mutation_payload", None)
    if isinstance(mutation_payload, dict):
        return dict(mutation_payload)
    path = str(getattr(request, "path", ""))
    query = path.split("?", 1)[1] if "?" in path else ""
    from urllib.parse import parse_qs
    return {key: values[-1] for key, values in parse_qs(query, keep_blank_values=True).items()}


def _text(payload: dict[str, Any], key: str, *, maximum: int) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:maximum]


def _normalize_api_base(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")].rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API base must be an http(s) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("API base must not contain credentials or a fragment")
    if any(ord(char) < 0x20 for char in value):
        raise ValueError("API base contains invalid characters")
    return value


def _provider_settings() -> dict[str, Any]:
    try:
        config = load_config(_config_path())
        provider = config.providers.custom
        api_base = resolve_env_refs(provider.api_base or "")
        api_key = resolve_env_refs(provider.api_key or "")
        model = str(resolve_env_refs(config.agents.defaults.model or "") or "")
        if model.startswith("custom/"):
            model = model[len("custom/"):]
        return {
            "apiBase": str(api_base or "").strip(),
            "model": model.strip(),
            "apiKeyConfigured": bool(str(api_key or "").strip()),
        }
    except Exception:
        return {
            "apiBase": os.getenv("LLM_BASE_URL", ""),
            "model": os.getenv("LLM_MODEL", ""),
            "apiKeyConfigured": bool(os.getenv("LLM_API_KEY", "").strip()),
        }


def _credentials(payload: dict[str, Any]) -> tuple[str, str, str]:
    current = _provider_settings()
    api_base = _normalize_api_base(_text(payload, "apiBase", maximum=_MAX_API_BASE) or str(current["apiBase"]))
    model = _text(payload, "model", maximum=_MAX_MODEL_ID) or str(current["model"])
    if not model:
        raise ValueError("model ID is required")
    api_key = _text(payload, "apiKey", maximum=_MAX_API_KEY)
    if not api_key:
        try:
            config = load_config(_config_path())
            api_key = str(resolve_env_refs(config.providers.custom.api_key or "") or "").strip()
        except Exception:
            api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise ValueError("API key is required")
    return api_base, model, api_key


def _provider_headers(api_key: str, *, api_key_header: bool = False) -> dict[str, str]:
    if api_key_header:
        return {"Accept": "application/json", "x-api-key": api_key}
    return {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}


def _request_with_auth_fallback(
    request: Callable[[dict[str, str]], httpx.Response],
    api_key: str,
) -> httpx.Response:
    """Keep Bearer auth first, then support proxies which require x-api-key."""
    response = request(_provider_headers(api_key))
    if response.status_code == 401:
        response = request(_provider_headers(api_key, api_key_header=True))
    return response


def _models_response(payload: dict[str, Any]) -> Response:
    try:
        api_base, _, api_key = _credentials(payload)
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = _request_with_auth_fallback(
                lambda headers: client.get(f"{api_base}/models", headers=headers),
                api_key,
            )
            response.raise_for_status()
        body = response.json()
        rows = body.get("data", []) if isinstance(body, dict) else []
        models: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip():
                    models.append(row["id"].strip())
        return http_json_response({"ok": True, "models": models[:500], "count": len(models)})
    except ValueError as exc:
        return http_error(400, str(exc))
    except httpx.HTTPStatusError as exc:
        return http_error(502, f"Model list request failed with HTTP {exc.response.status_code}.")
    except (httpx.HTTPError, ValueError) as exc:
        return http_error(502, f"Could not load models: {type(exc).__name__}")


def _test_response(payload: dict[str, Any]) -> Response:
    try:
        api_base, model, api_key = _credentials(payload)
        with httpx.Client(timeout=45.0, follow_redirects=False) as client:
            response = _request_with_auth_fallback(
                lambda headers: client.post(
                    f"{api_base}/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                        "max_tokens": 16,
                        "temperature": 0,
                    },
                ),
                api_key,
            )
            response.raise_for_status()
        body = response.json()
        choice = body.get("choices", [{}])[0] if isinstance(body, dict) else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        return http_json_response({"ok": True, "model": model, "response": str(content or "").strip()[:500]})
    except ValueError as exc:
        return http_error(400, str(exc))
    except httpx.HTTPStatusError as exc:
        return http_error(502, f"Provider test failed with HTTP {exc.response.status_code}.")
    except (httpx.HTTPError, ValueError) as exc:
        return http_error(502, f"Provider test failed: {type(exc).__name__}")


def _save_response(
    payload: dict[str, Any],
    *,
    refresh_runtime_config: Callable[[], Any] | None,
) -> Response:
    try:
        api_base = _normalize_api_base(_text(payload, "apiBase", maximum=_MAX_API_BASE))
        model = _text(payload, "model", maximum=_MAX_MODEL_ID)
        api_key = _text(payload, "apiKey", maximum=_MAX_API_KEY)
        if not model:
            raise ValueError("model ID is required")
        config_path = _config_path()
        config = load_config(config_path)
        config.providers.custom.api_base = api_base
        if api_key:
            config.providers.custom.api_key = api_key
        config.agents.defaults.model = f"custom/{model}"
        save_config(config, config_path)
        if refresh_runtime_config is not None:
            refresh_runtime_config()
        return http_json_response({"ok": True, **_provider_settings()})
    except ValueError as exc:
        return http_error(400, str(exc))
    except OSError:
        return http_error(500, "Could not save provider settings")
    except Exception as exc:
        return http_error(500, f"Could not apply provider settings: {type(exc).__name__}")


def _execution_settings() -> dict[str, Any]:
    try:
        config = apply_render_execution_env(load_config(_config_path()))
        execution = config.execution
        vps = execution.vps
        return {
            "backend": execution.backend,
            "vps": {
                "host": vps.host,
                "port": vps.port,
                "username": vps.username,
                "host_key_fingerprint": vps.host_key_fingerprint,
                "host_key_policy": vps.host_key_policy,
                "workspace_dir": vps.workspace_dir,
                "connect_timeout": vps.connect_timeout,
                "passwordConfigured": bool(vps.password.strip()),
                "privateKeyConfigured": bool(vps.private_key.strip()),
            },
        }
    except Exception:
        return {
            "backend": "novita",
            "vps": {
                "host": "",
                "port": 22,
                "username": "",
                "host_key_fingerprint": "",
                "host_key_policy": "fingerprint",
                "workspace_dir": "/workspace",
                "connect_timeout": 15,
                "passwordConfigured": False,
                "privateKeyConfigured": False,
            },
        }


def _save_execution_settings(
    payload: dict[str, Any],
    *,
    refresh_runtime_config: Callable[[], Any] | None,
) -> Response:
    try:
        backend_name = _text(payload, "backend", maximum=20).lower() or "novita"
        if backend_name not in {"novita", "vps"}:
            raise ValueError("execution backend must be novita or vps")
        config_path = _config_path()
        config = apply_render_execution_env(load_config(config_path))
        vps = config.execution.vps
        host = _text(payload, "host", maximum=253)
        username = _text(payload, "username", maximum=64)
        fingerprint = _text(payload, "hostKeyFingerprint", maximum=256)
        policy = _text(payload, "hostKeyPolicy", maximum=20) or "fingerprint"
        workspace = validate_vps_workspace(_text(payload, "workspaceDir", maximum=256) or "/workspace")
        if host:
            host = validate_vps_host(host)
        if username:
            username = validate_vps_username(username)
        if fingerprint:
            fingerprint = validate_vps_fingerprint(fingerprint)
        if policy not in {"fingerprint", "accept_any"}:
            raise ValueError("host key policy must be fingerprint or accept_any")
        try:
            port = int(_text(payload, "port", maximum=6) or vps.port)
            connect_timeout = int(_text(payload, "connectTimeout", maximum=3) or vps.connect_timeout)
        except ValueError:
            raise ValueError("port and connect timeout must be integers") from None
        if not 1 <= port <= 65535:
            raise ValueError("VPS SSH port must be between 1 and 65535")
        if not 1 <= connect_timeout <= 60:
            raise ValueError("connect timeout must be between 1 and 60 seconds")
        password = _text(payload, "password", maximum=4096)
        private_key = normalize_vps_private_key(
            _text(payload, "privateKey", maximum=32768)
        )
        vps.host = host
        vps.port = port
        vps.username = username
        vps.host_key_fingerprint = fingerprint
        vps.host_key_policy = policy
        vps.workspace_dir = workspace
        vps.connect_timeout = connect_timeout
        if password:
            vps.password = password
        if private_key:
            vps.private_key = private_key
        config.execution.backend = backend_name
        if backend_name == "vps":
            VPSExecutionBackend(vps)._validate()
        save_config(config, config_path)
        with suppress(OSError):
            os.chmod(config_path, 0o600)
        if refresh_runtime_config is not None:
            refresh_runtime_config()
        return http_json_response({"ok": True, **_execution_settings()})
    except ValueError as exc:
        return http_error(400, str(exc))
    except Exception as exc:
        return http_error(500, f"Could not save execution settings: {type(exc).__name__}")


def _run_vps_test(config: Any) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(VPSExecutionBackend(config).test_connection()))
        except BaseException as exc:  # propagate only a sanitized type below
            error.append(exc)

    thread = threading.Thread(target=runner, name="vps-connection-test", daemon=True)
    thread.start()
    thread.join(timeout=75)
    if thread.is_alive():
        raise TimeoutError("VPS connection test timed out")
    if error:
        exc = error[0]
        detail = " ".join(str(exc).split())[:320] or "no diagnostic detail"
        raise RuntimeError(f"{type(exc).__name__}: {detail}") from exc
    return result[0] if result else {"ok": False}


def _execution_test_config(payload: dict[str, Any], config: Any) -> tuple[str, Any]:
    backend_name = _text(payload, "backend", maximum=20).lower() or config.execution.backend
    if backend_name not in {"novita", "vps"}:
        raise ValueError("execution backend must be novita or vps")
    vps = config.execution.vps.model_copy(deep=True)
    host = _text(payload, "host", maximum=253) or vps.host
    username = _text(payload, "username", maximum=64) or vps.username
    fingerprint = _text(payload, "hostKeyFingerprint", maximum=256) or vps.host_key_fingerprint
    policy = _text(payload, "hostKeyPolicy", maximum=20) or vps.host_key_policy or "fingerprint"
    if host:
        host = validate_vps_host(host)
    if username:
        username = validate_vps_username(username)
    if fingerprint:
        fingerprint = validate_vps_fingerprint(fingerprint)
    if policy not in {"fingerprint", "accept_any"}:
        raise ValueError("host key policy must be fingerprint or accept_any")
    try:
        port = int(_text(payload, "port", maximum=6) or vps.port)
        connect_timeout = int(_text(payload, "connectTimeout", maximum=3) or vps.connect_timeout)
    except ValueError:
        raise ValueError("port and connect timeout must be integers") from None
    if not 1 <= port <= 65535:
        raise ValueError("VPS SSH port must be between 1 and 65535")
    if not 1 <= connect_timeout <= 60:
        raise ValueError("connect timeout must be between 1 and 60 seconds")
    password = _text(payload, "password", maximum=4096)
    private_key = _text(payload, "privateKey", maximum=32768)
    vps.host = host
    vps.port = port
    vps.username = username
    vps.host_key_fingerprint = fingerprint
    vps.host_key_policy = policy
    vps.workspace_dir = validate_vps_workspace(_text(payload, "workspaceDir", maximum=256) or vps.workspace_dir)
    vps.connect_timeout = connect_timeout
    if password:
        vps.password = password
    if private_key:
        vps.private_key = private_key
    return backend_name, vps


def _test_execution_response(payload: dict[str, Any] | None = None) -> Response:
    try:
        config = apply_render_execution_env(load_config(_config_path()))
        backend_name, vps = _execution_test_config(payload or {}, config)
        if backend_name != "vps":
            return http_json_response({"ok": True, "backend": "novita", "message": "Novita Sandbox is selected."})
        tested = _run_vps_test(vps)
        return http_json_response({"ok": bool(tested.get("ok")), "backend": "vps", **tested})
    except ValueError as exc:
        return http_error(400, str(exc))
    except RuntimeError as exc:
        return http_error(502, f"VPS connection test failed: {str(exc)}")
    except Exception as exc:
        detail = " ".join(str(exc).split())[:320] or type(exc).__name__
        return http_error(502, f"VPS connection test failed: {detail}")


def _dbq_admin_section() -> str:
    return """<section><h2>University database workspace</h2><p class='hint'>Admin-only DBQ workspace. Load the live catalogue to inspect all portal tables, view student records, update verified scores, publish results, and perform guarded generic insert/update/delete operations. Database credentials remain server-side and are never returned to this page.</p><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'><label>Catalogue search<input id='dbqSearch' placeholder='student, payment, result...'></label><label>Selected table<select id='dbqTable'><option value=''>Load catalogue first</option></select></label><label>Rows per page<input id='dbqLimit' type='number' min='1' max='200' value='50'></label></div><h3>Find a person or record</h3><p class='hint'>Search names, registration numbers, staff IDs, email addresses, course codes, statuses, dates, and other searchable fields. Results remain inside the admin workspace.</p><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'><label>Person search<input id='dbqPersonTerm' placeholder='Name, regno, staff ID or email'></label><label>Person scope<select id='dbqPersonScope'><option value='all'>Students and staff</option><option value='students'>Students only</option><option value='staff'>Staff directory</option></select></label></div><button id='dbqPersonSearch' class='secondary'>Search person</button><pre id='dbqPersonStatus' class='hint'>No person search performed.</pre><div id='dbqPersonResults' style='overflow:auto;max-height:26rem'></div><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'><label>Search selected table<input id='dbqTableTerm' placeholder='Name, code, ID or status'></label><button id='dbqTableSearch' class='secondary'>Search selected table</button></div><pre id='dbqTableSearchStatus' class='hint'>Select a table, then search its searchable fields.</pre><div id='dbqTableSearchResults' style='overflow:auto;max-height:26rem'></div><button id='dbqLoadCatalog' class='secondary'>Load all tables</button><button id='dbqLoadWorkspaces' class='secondary'>Load student/staff/course workspaces</button><pre id='dbqWorkspaces' class='hint'>No portal workspaces loaded.</pre><button id='dbqPing' class='secondary'>Test database connection</button><button id='dbqLoadSchema' class='secondary'>Load selected schema</button><button id='dbqLoadRows' class='secondary'>Read selected rows</button><button id='dbqPrevRows' class='secondary'>Previous rows</button><button id='dbqNextRows' class='secondary'>Next rows</button><span id='dbqPageInfo' class='hint'>Page 1</span><h3>Result and course diagnostics</h3><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.5rem'><label>Map programme<input id='dbqMapProg' value='ELC'></label><label>Map portal category<input id='dbqMapCategory' value='ug'></label></div><button id='dbqBatchCheck' class='secondary'>Batch-check course results</button><button id='dbqMapCheck' class='secondary'>Check course map</button><pre id='dbqDiagnostics' class='hint'>No diagnostics loaded.</pre><pre id='dbqStatus' class='hint'>No database request made.</pre><div style='overflow:auto;max-height:28rem'><table><thead><tr><th>Table</th><th>Purpose</th><th>Type</th><th>Rows</th><th>Write guidance</th></tr></thead><tbody id='dbqTableRows'><tr><td colspan='5'>Load the catalogue to begin.</td></tr></tbody></table></div><h3>Schema and row data</h3><div id='dbqSchemaView' style='overflow:auto;max-height:24rem'><p class='hint'>Load a selected schema to see every field, type, key, nullability and role.</p></div><pre id='dbqSchema' class='hint'>No schema loaded.</pre><div id='dbqRowsView' style='overflow:auto;max-height:32rem'><p class='hint'>Read selected rows to display an editable row list.</p></div><div id='dbqFieldEditor' style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'><p class='hint'>Click Edit row to open a field-by-field editor. Primary keys remain fixed.</p></div><pre id='dbqRows' class='hint'>No rows loaded.</pre><h3>Student and scores</h3><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'><label>Registration number<input id='dbqRegno' placeholder='22/205EEE/132'></label><label>Session<input id='dbqSession' placeholder='2025/2026'></label></div><button id='dbqStudentLookup' class='secondary'>View student and scores</button><pre id='dbqStudent' class='hint'>No student loaded.</pre><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.5rem'><label>Course<input id='dbqCourse' placeholder='FEG412'></label><label>Semester<input id='dbqSemester' placeholder='1st'></label><label>CA<input id='dbqCA' type='number' min='0' max='100'></label><label>Exam<input id='dbqExam' type='number' min='0' max='100'></label><label>Operator<input id='dbqOperator' placeholder='ACA2538'></label><label>Modifier<input id='dbqModifier' placeholder='ACA_ADMIN'></label></div><label><input id='dbqPublish' type='checkbox'> Publish score row after update</label><button id='dbqScoreUpdate'>Update verified score</button><button id='dbqScorePublish' class='secondary'>Publish selected score row</button><h3>Student account access</h3><p class='hint'>Look up a student’s non-secret account status or reset the portal password. Password values are never displayed, returned, or placed in the generic table editor.</p><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'><label>Student registration number<input id='dbqAccountRegno' placeholder='22/205EEE/132'></label><label>New password<input id='dbqStudentNewPassword' type='password' minlength='8' maxlength='128' autocomplete='new-password' placeholder='At least 8 characters'></label><label>Confirm new password<input id='dbqStudentConfirmPassword' type='password' minlength='8' maxlength='128' autocomplete='new-password'></label><label>Admin operator<input id='dbqStudentAccountOperator' placeholder='ACA_ADMIN'></label><label>Reset reason or ticket<input id='dbqStudentResetReason' placeholder='Verified support request'></label></div><button id='dbqStudentAccountLookup' class='secondary'>View student account status</button><label><input id='dbqStudentResetConfirm' type='checkbox'> I confirmed the student identity and reset request</label><button id='dbqStudentPasswordReset'>Reset student password</button><pre id='dbqStudentAccountStatus' class='hint'>No student account operation performed.</pre><h3>Payments and eligibility</h3><p class='hint'>Payment writes require verified evidence, a non-placeholder fee code, matching detail totals, and explicit confirmation. Use the read/check actions before any write.</p><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.5rem'><label>Payment transaction ID<input id='dbqTransId' placeholder='Optional for check; required for add'></label><label>Amount<input id='dbqPaymentAmount' type='number' step='0.01'></label><label>Receipt number<input id='dbqReceipt'></label><label>RRR<input id='dbqRRR'></label><label>Payment date<input id='dbqPaymentDate' type='date'></label><label>Payment time<input id='dbqPaymentTime' type='time' step='1'></label><label>Pay item ID<input id='dbqPayItemId'></label><label>Fee code<input id='dbqFeeCode'></label><label>Programme<input id='dbqPaymentProg'></label><label>Level<input id='dbqPaymentLevel'></label><label>Modifier<input id='dbqPaymentModifier'></label><label>Verification reference<input id='dbqVerificationRef' placeholder='Bank evidence reference'></label></div><label>Payment detail JSON<textarea id='dbqPaymentDetails' rows='4' placeholder='[{"folio_code":"TUITION","amount":"100000.00"}]'></textarea></label><button id='dbqLoadStudentPayments' class='secondary'>View student payments</button><button id='dbqPaymentCheck' class='secondary'>Check payment</button><button id='dbqEligibility' class='secondary'>Check eligibility</button><button id='dbqPaymentAdd'>Add verified payment</button><button id='dbqPaymentReconcile' class='secondary'>Reconcile payment</button><pre id='dbqPayment' class='hint'>No payment operation performed.</pre><h4>Edit a payment record</h4><p class='hint'>Choose a loaded record and edit its labeled fields below. No JSON or coding is required. Every edit still requires an operator, verification reference, before/after verification, and a confirmation. Amount changes additionally require matching detail totals.</p><div id='dbqPaymentRecordCards' style='overflow:auto;max-height:28rem'><p class='hint'>Load student payments to see main and detail records with Edit buttons.</p></div><div id='dbqPaymentManualEditor' style='display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.5rem'><p class='hint'>Select Edit on a payment record to open its labeled fields.</p></div><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.5rem'><label>Record type<select id='dbqPaymentRecordType'><option value='main'>Main payment</option><option value='detail'>Payment detail</option></select></label><label>Record ID<input id='dbqPaymentRecordId' type='number' min='1'></label><label>Edit operator<input id='dbqPaymentEditOperator'></label></div><label>Verification reference<input id='dbqPaymentVerificationRef' placeholder='Bank evidence reference'></label><label><input id='dbqPaymentConfirmAmount' type='checkbox'> I independently verified any amount change</label><button id='dbqPaymentManualUpdate'>Update selected payment</button><details><summary>Advanced JSON editor</summary><label>Changes JSON<textarea id='dbqPaymentChanges' rows='4' placeholder='{"fee_code":"verified-code","response_desc":"Verified by bursary"}'></textarea></label><button id='dbqPaymentUpdate'>Update payment record from JSON</button></details><pre id='dbqPaymentRows' class='hint'>No student payments loaded.</pre><h3>Generic table operations</h3><p class='hint'>Use JSON objects. Keys must identify a primary-key row; updates never change primary-key columns. Delete requires a second confirmation.</p><label>Primary-key JSON<input id='dbqKeys' placeholder='{"id":123}'></label><label>Values JSON<textarea id='dbqValues' rows='4' placeholder='{"status":"Active"}'></textarea></label><button id='dbqInsert' class='secondary'>Insert row</button><button id='dbqUpdate'>Update row</button><button id='dbqDelete' class='secondary'>Delete row</button><pre id='dbqGeneric' class='hint'>No generic row operation performed.</pre></section><script>(()=>{const $=id=>document.getElementById(id);const status=(text,ok=true)=>{$('dbqStatus').textContent=text;$('dbqStatus').style.color=ok?'#a7f3d0':'#fca5a5';};const adminRequest=(action,payload)=>{if(typeof window.nanobotAdminRequest!=='function')throw new Error('Admin connection is not ready');return window.nanobotAdminRequest(action,payload);};const table=()=>$('dbqTable').value.trim();const json=(id,fallback={})=>{const raw=$(id).value.trim();if(!raw)return fallback;const value=JSON.parse(raw);if(!value||typeof value!=='object'||Array.isArray(value))throw new Error(`${id} must be a JSON object`);return value;};const get=async(path,params={})=>{const query=new URLSearchParams(Object.entries(params).filter(([,v])=>String(v||'').trim()).map(([k,v])=>[k,String(v)]));const r=await fetch(`${path}?${query}`,{cache:'no-store'});const text=await r.text();let body;try{body=JSON.parse(text);}catch{body={error:text.slice(0,300)||`Invalid server response (HTTP ${r.status})`};}if(!r.ok)throw new Error(body.error||`Request failed: ${r.status}`);return body;};const loadWorkspaces=async()=>{status('Loading portal workspaces...');try{const v=await get('/api/admin/dbq/workspaces',{search:$('dbqSearch').value});const root=$('dbqWorkspaces');root.replaceChildren(...(v.workspaces||[]).map(group=>{const box=document.createElement('div');box.style.marginBottom='.75rem';const title=document.createElement('strong');title.textContent=`${group.title} (${group.tables?.length||0})`;box.append(title);const note=document.createElement('div');note.className='hint';note.textContent=group.description||'';box.append(note);const list=document.createElement('div');list.style.display='flex';list.style.flexWrap='wrap';list.style.gap='.25rem';(group.tables||[]).forEach(item=>{const button=document.createElement('button');button.className='secondary';button.textContent=String(item.name||'');button.title=String(item.purpose||item.handles||'');button.onclick=()=>{const select=$('dbqTable');const name=String(item.name||'');if(![...select.options].some(option=>option.value===name))select.add(new Option(name,name));select.value=name;dbqOffset=0;void loadSchema();void loadRows();};list.append(button);});box.append(list);return box;}));status(`Loaded ${v.table_count||0} table(s) into ${(v.workspaces||[]).length} workspace(s).`);}catch(e){status(e.message,false);}};const renderSearchResults=(rootId,matches)=>{const root=$(rootId);root.replaceChildren();const flat=[];(matches||[]).forEach(match=>(match.rows||[]).forEach(row=>flat.push({table:match.table,row})));if(!flat.length){root.textContent='No matching rows found.';return;}const tableEl=document.createElement('table');const head=document.createElement('tr');['Table','Match','Action'].forEach(label=>{const th=document.createElement('th');th.textContent=label;head.append(th);});const thead=document.createElement('thead');thead.append(head);tableEl.append(thead);const body=document.createElement('tbody');flat.forEach(item=>{const tr=document.createElement('tr');const tableCell=document.createElement('td');tableCell.textContent=item.table;tr.append(tableCell);const matchCell=document.createElement('td');const summary=Object.entries(item.row).slice(0,6).map(([key,value])=>`${key}: ${value??''}`).join(' | ');matchCell.textContent=summary;tr.append(matchCell);const actionCell=document.createElement('td');const open=document.createElement('button');open.className='secondary';open.textContent='Open row';open.onclick=async()=>{const select=$('dbqTable');if(![...select.options].some(option=>option.value===item.table))select.add(new Option(item.table,item.table));select.value=item.table;dbqOffset=0;await loadSchema();renderRows([item.row]);renderFieldEditor(item.row);status(`Opened ${item.table} search result in the row editor.`);};actionCell.append(open);tr.append(actionCell);body.append(tr);});tableEl.append(body);root.append(tableEl);};const searchPerson=async()=>{const term=$('dbqPersonTerm').value.trim();if(term.length<2)return $('dbqPersonStatus').textContent='Enter at least 2 search characters.';$('dbqPersonStatus').textContent='Searching students and staff...';try{const value=await get('/api/admin/dbq/person-search',{term,scope:$('dbqPersonScope').value,limit:Math.min(100,Math.max(1,Number($('dbqLimit').value)||50))});renderSearchResults('dbqPersonResults',value.matches);const count=(value.matches||[]).reduce((total,item)=>total+(item.rows||[]).length,0);$('dbqPersonStatus').textContent=`Found ${count} matching row(s) across ${(value.matches||[]).length} table(s). Tap Open row to inspect or edit.`;}catch(e){$('dbqPersonStatus').textContent=e.message;}};const searchTable=async()=>{if(!table())return $('dbqTableSearchStatus').textContent='Select a table first.';const term=$('dbqTableTerm').value.trim();if(term.length<2)return $('dbqTableSearchStatus').textContent='Enter at least 2 search characters.';$('dbqTableSearchStatus').textContent=`Searching ${table()}...`;try{const value=await get('/api/admin/dbq/table-search',{table:table(),term,limit:Math.min(100,Math.max(1,Number($('dbqLimit').value)||50))});renderSearchResults('dbqTableSearchResults',[value]);$('dbqTableSearchStatus').textContent=`Found ${(value.rows||[]).length} matching row(s) in ${value.table}. Search columns: ${(value.search_columns||[]).join(', ')||'none'}.`;}catch(e){$('dbqTableSearchStatus').textContent=e.message;}};$('dbqPersonSearch').onclick=searchPerson;$('dbqTableSearch').onclick=searchTable;const loadCatalog=async()=>{status('Loading all database tables...');try{const v=await get('/api/admin/dbq/catalog',{search:$('dbqSearch').value});const select=$('dbqTable');select.replaceChildren(new Option('Select a table',''),...(v.tables||[]).map(t=>new Option(`${t.TABLE_NAME} — ${t.purpose||t.TABLE_COMMENT||t.TABLE_TYPE||''}`,t.TABLE_NAME)));$('dbqTableRows').replaceChildren(...(v.tables||[]).map(t=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${String(t.TABLE_NAME||'')}</td><td>${String(t.purpose||t.TABLE_COMMENT||'')}</td><td>${String(t.TABLE_TYPE||'')}</td><td>${String(t.TABLE_ROWS??'')}</td><td>${t.can_update_delete?'Primary-key update/delete available':'Inspect-only update/delete disabled without primary key'}</td>`;tr.onclick=()=>{select.value=String(t.TABLE_NAME||'');dbqOffset=0;void loadSchema();void loadRows();};return tr;}));status(`Loaded ${v.table_count||0} table(s).`);}catch(e){status(e.message,false);}};let currentSchema=null;let dbqOffset=0;const renderSchema=(value)=>{const root=$('dbqSchemaView');root.replaceChildren();const columns=value.columns||[];if(!columns.length){root.textContent='No columns returned.';return;}const tableEl=document.createElement('table');const head=document.createElement('tr');['Field','Type','Key','Nullable','Default','Role'].forEach(label=>{const th=document.createElement('th');th.textContent=label;head.append(th);});const thead=document.createElement('thead');thead.append(head);tableEl.append(thead);const body=document.createElement('tbody');columns.forEach(col=>{const tr=document.createElement('tr');[col.COLUMN_NAME,col.COLUMN_TYPE,col.COLUMN_KEY||'',col.IS_NULLABLE,col.COLUMN_DEFAULT??'',col.role||''].forEach(value=>{const td=document.createElement('td');td.textContent=String(value);tr.append(td);});body.append(tr);});tableEl.append(body);root.append(tableEl);};const renderFieldEditor=(row)=>{const root=$('dbqFieldEditor');root.replaceChildren();if(!currentSchema||!row){root.textContent='Click Edit row to open a field-by-field editor.';return;}const primary=new Set(currentSchema.primary_columns||[]);const columns=currentSchema.columns||[];const title=document.createElement('strong');title.textContent=`Editing ${table()} row`;root.append(title);const inputs=new Map();columns.forEach(col=>{const name=String(col.COLUMN_NAME||'');const label=document.createElement('label');label.textContent=`${name}${primary.has(name)?' (primary key)':''}`;const type=String(col.COLUMN_TYPE||'').toLowerCase();const input=document.createElement(type.includes('text')||type.includes('json')||type.includes('blob')?'textarea':'input');if(input.tagName==='INPUT'&&type.includes('int'))input.type='number';input.value=row[name]===null||row[name]===undefined?'':String(row[name]);input.disabled=primary.has(name);input.title=String(col.role||'');label.append(input);root.append(label);inputs.set(name,input);});if(!currentSchema.primary_columns?.length){const note=document.createElement('p');note.className='hint';note.textContent='This table has no primary key; safe row update/delete is disabled, but all fields remain visible for inspection.';root.append(note);return;}const save=document.createElement('button');save.textContent='Update selected row from fields';save.onclick=async()=>{try{const keys=Object.fromEntries((currentSchema.primary_columns||[]).map(name=>[name,row[name]]));const values={};inputs.forEach((input,name)=>{if(!primary.has(name)&&input.value!==String(row[name]??''))values[name]=input.value;});if(!Object.keys(values).length)return status('No field changes detected.',false);if(!confirm(`Update this row in ${table()}?`))return;status('Updating selected row...');const result=await adminRequest('admin.dbq.execute',{operation:'update',table:table(),keys,values,confirmed:true});$('dbqGeneric').textContent=JSON.stringify(result,null,2);status('Row updated and verified.');void loadRows();}catch(e){status(e.message,false);}};root.append(save);};const renderRows=(rows)=>{const root=$('dbqRowsView');root.replaceChildren();if(!rows.length){root.textContent='No rows returned for this page.';return;}const columns=[...new Set(rows.flatMap(row=>Object.keys(row)))];const tableEl=document.createElement('table');const head=document.createElement('tr');const actionHead=document.createElement('th');actionHead.textContent='Editor';head.append(actionHead);columns.forEach(name=>{const th=document.createElement('th');th.textContent=name;head.append(th);});const thead=document.createElement('thead');thead.append(head);tableEl.append(thead);const body=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');const action=document.createElement('td');const edit=document.createElement('button');edit.className='secondary';edit.textContent='Edit row';edit.onclick=()=>{const keys=Object.fromEntries((currentSchema?.primary_columns||[]).map(key=>[key,row[key]]));const values={...row};Object.keys(keys).forEach(key=>delete values[key]);$('dbqKeys').value=JSON.stringify(keys,null,2);$('dbqValues').value=JSON.stringify(values,null,2);renderFieldEditor(row);status('Loaded this row into the guarded editor. Review values before updating.',true);};action.append(edit);tr.append(action);columns.forEach(name=>{const td=document.createElement('td');td.textContent=String(row[name]??'');tr.append(td);});body.append(tr);});tableEl.append(body);root.append(tableEl);};const loadSchema=async()=>{if(!table())return status('Select a table first.',false);status('Loading schema...');try{const v=await get('/api/admin/dbq/schema',{table:table()});currentSchema=v;$('dbqSchema').textContent=JSON.stringify(v,null,2);renderSchema(v);$('dbqKeys').value=v.primary_columns?.length?JSON.stringify(Object.fromEntries(v.primary_columns.map(k=>[k,'']))):'';status(`Loaded ${v.columns?.length||0} field(s) for ${table()}.`);}catch(e){status(e.message,false);}};const loadRows=async()=>{if(!table())return status('Select a table first.',false);status('Reading rows...');try{const limit=$('dbqLimit').value;const v=await get('/api/admin/dbq/read',{table:table(),limit,offset:dbqOffset});$('dbqRows').textContent=JSON.stringify(v.rows||[],null,2);renderRows(v.rows||[]);$('dbqPageInfo').textContent=`Rows ${dbqOffset+1}-${dbqOffset+(v.rows||[]).length} (offset ${dbqOffset})`;status(`Read ${(v.rows||[]).length} row(s) from ${table()}.`);}catch(e){status(e.message,false);}};const diagnostics=async(kind)=>{if(!$('dbqCourse').value.trim()||!$('dbqSession').value.trim())return status('Enter course and session first.',false);status(`${kind}...`);try{const path=kind==='batch_check'?'/api/admin/dbq/batch-check':'/api/admin/dbq/map-check';const params={course:$('dbqCourse').value,session:$('dbqSession').value,semester:$('dbqSemester').value};if(kind==='map_check'){params.prog=$('dbqMapProg').value;params.portalCategory=$('dbqMapCategory').value;}const v=await get(path,params);$('dbqDiagnostics').textContent=JSON.stringify(v,null,2);status(`${kind} completed.`);}catch(e){status(e.message,false);}};const loadStudent=async()=>{status('Loading student and scores...');try{const v=await get('/api/admin/dbq/student',{regno:$('dbqRegno').value,session:$('dbqSession').value});$('dbqStudent').textContent=JSON.stringify(v,null,2);const first=(v.results||[])[0];if(first){$('dbqCourse').value=first.course_code||'';$('dbqSemester').value=first.semester||'';$('dbqCA').value=first.decoded_ca??first.final_ca??'';$('dbqExam').value=first.decoded_exam??first.final_exam??'';}const resultMessage=v.result_status==='unavailable'?`Student loaded, but results are unavailable: ${v.result_error||'result query failed'}.`:(v.student?`Loaded ${v.results?.length||0} result row(s).`:'Student not found.');status(resultMessage,Boolean(v.student)&&v.result_status!=='unavailable');}catch(e){status(e.message,false);}};const accountRegno=()=>($('dbqAccountRegno').value.trim()||$('dbqRegno').value.trim());const loadStudentAccount=async()=>{const regno=accountRegno();if(!regno){$('dbqStudentAccountStatus').textContent='Enter a student registration number first.';return;}status('Loading student account status...');try{const v=await adminRequest('admin.dbq.execute',{operation:'student_account_lookup',regno});$('dbqStudentAccountStatus').textContent=JSON.stringify(v.student||{message:'Student account not found.'},null,2);status(v.student?`Loaded account status for ${regno}.`:'Student account not found.',Boolean(v.student));}catch(e){$('dbqStudentAccountStatus').textContent=e.message;status(e.message,false);}};const resetStudentPassword=async()=>{const regno=accountRegno();const newPassword=$('dbqStudentNewPassword').value;const confirmPassword=$('dbqStudentConfirmPassword').value;const operator=$('dbqStudentAccountOperator').value.trim();const reason=$('dbqStudentResetReason').value.trim();const confirmed=$('dbqStudentResetConfirm').checked;if(!regno||!newPassword||!confirmPassword||!operator||!reason){status('Enter the student, both password fields, operator, and reset reason.',false);return;}if(!confirmed){status('Confirm the student identity and reset request first.',false);return;}if(!window.confirm(`Reset the portal password for ${regno}?`))return;status('Resetting student password...');try{const v=await adminRequest('admin.dbq.execute',{operation:'student_password_reset',regno,newPassword,confirmPassword,operator,reason,confirmed:true});$('dbqStudentAccountStatus').textContent=v.message||'Student password reset completed.';$('dbqStudentNewPassword').value='';$('dbqStudentConfirmPassword').value='';$('dbqStudentResetConfirm').checked=false;status(v.message||'Student password reset completed.');}catch(e){status(e.message,false);}};const payment=()=>{let details=[];const raw=$('dbqPaymentDetails').value.trim();if(raw)details=JSON.parse(raw);return {regno:$('dbqRegno').value,session:$('dbqSession').value,semester:$('dbqSemester').value,transId:$('dbqTransId').value,amount:$('dbqPaymentAmount').value,receipt:$('dbqReceipt').value,rrr:$('dbqRRR').value,paymentDate:$('dbqPaymentDate').value,paymentTime:$('dbqPaymentTime').value,payItemId:$('dbqPayItemId').value,feeCode:$('dbqFeeCode').value,prog:$('dbqPaymentProg').value,level:$('dbqPaymentLevel').value,modifier:$('dbqPaymentModifier').value,operator:$('dbqOperator').value,verificationRef:$('dbqVerificationRef').value,details};};const manualPaymentFields=type=>type==='main'?['amount','receipt_number','payment_desc','pay_item_id','payment_date','payment_time','response_code','response_desc','session','semester','prog_id','level','rrr','channel','fee_code','portal_category_code','payment_status']:['folio_code','amount','session','level','prog_id','stud_category','studstatus','portal_category_code','payment_date','payment_time','trans_id','rrr','pay_item_id'];let manualPaymentRow=null;const displayField=name=>name.replaceAll('_',' ').replace(/\\b\\w/g,letter=>letter.toUpperCase());const renderPaymentEditor=(type,row)=>{manualPaymentRow={type,row};$('dbqPaymentRecordType').value=type;$('dbqPaymentRecordId').value=row.id??'';const root=$('dbqPaymentManualEditor');root.replaceChildren();manualPaymentFields(type).forEach(field=>{const label=document.createElement('label');label.textContent=displayField(field);const input=document.createElement('input');input.id=`dbqManualPayment_${field}`;input.type=field==='amount'?'number':field==='payment_date'?'date':field==='payment_time'?'time':'text';if(field==='amount')input.step='0.01';input.value=row[field]??'';label.append(input);root.append(label);});$('dbqPaymentEditOperator').value=$('dbqPaymentEditOperator').value||$('dbqOperator').value||'';status(`Editing ${type} payment record ${row.id??''}.`);};const renderPaymentRecordCards=value=>{const root=$('dbqPaymentRecordCards');root.replaceChildren();const add=(type,row)=>{const card=document.createElement('div');card.style.cssText='border:1px solid #334155;border-radius:.5rem;padding:.6rem;margin:.35rem 0';const summary=[`#${row.id??''}`,row.regno||'',row.amount??row.folio_code??'',row.trans_id||row.rrr||'',row.payment_status||''].filter(Boolean).join(' | ');const text=document.createElement('span');text.textContent=`${type==='main'?'Main payment':'Payment detail'}: ${summary}`;const button=document.createElement('button');button.className='secondary';button.textContent='Edit fields';button.onclick=()=>renderPaymentEditor(type,row);card.append(text,button);root.append(card);};(value.payments||[]).forEach(row=>add('main',row));(value.details||[]).forEach(row=>add('detail',row));if(!root.children.length)root.textContent='No payment records found for this student and payment context.';};const manualPaymentUpdate=async()=>{try{if(!manualPaymentRow)throw new Error('Load payments and select Edit fields first.');if(!confirm(`Update ${manualPaymentRow.type} payment record ${manualPaymentRow.row.id??''}?`))return;const changes={};manualPaymentFields(manualPaymentRow.type).forEach(field=>{const input=$(`dbqManualPayment_${field}`);if(!input)return;const value=input.value;const old=manualPaymentRow.row[field]??'';if(String(value)!==String(old))changes[field]=value;});const verification=$('dbqPaymentVerificationRef').value.trim();if(!verification)throw new Error('Verification reference is required.');const operator=$('dbqPaymentEditOperator').value.trim()||$('dbqOperator').value.trim();const v=await adminRequest('admin.dbq.execute',{operation:'payment_update',recordType:manualPaymentRow.type,recordId:$('dbqPaymentRecordId').value,changes,confirmAmount:$('dbqPaymentConfirmAmount').checked,operator,verificationRef:verification});$('dbqPayment').textContent=JSON.stringify(v,null,2);status('Payment record updated and verified.');await loadStudentPayments();}catch(e){status(e.message,false);}};const loadStudentPayments=async()=>{status('Loading student payments...');try{const v=await get('/api/admin/dbq/student-payments',{regno:$('dbqRegno').value,session:$('dbqSession').value,semester:$('dbqSemester').value});$('dbqPaymentRows').textContent=JSON.stringify(v,null,2);renderPaymentRecordCards(v);const first=(v.payments||[])[0];if(first)renderPaymentEditor('main',first);status(`Loaded ${(v.payments||[]).length} main payment row(s) and ${(v.details||[]).length} detail row(s).`);}catch(e){status(e.message,false);}};const paymentAction=async(operation,confirmWrite=false)=>{try{if(confirmWrite&&!confirm(`${operation} payment record?`))return;const p=payment();p.operation=operation;status(`${operation}...`);const v=await adminRequest('admin.dbq.execute',p);$('dbqPayment').textContent=JSON.stringify(v,null,2);status(`${operation} completed.`);}catch(e){status(e.message,false);}};$('dbqLoadStudentPayments').onclick=loadStudentPayments;$('dbqPaymentManualUpdate').onclick=manualPaymentUpdate;$('dbqPaymentCheck').onclick=()=>paymentAction('payment_check');$('dbqEligibility').onclick=()=>paymentAction('eligibility_check');$('dbqPaymentAdd').onclick=()=>paymentAction('payment_add',true);$('dbqPaymentReconcile').onclick=()=>paymentAction('payment_reconcile',true);$('dbqPaymentUpdate').onclick=async()=>{try{if(!confirm('Update this payment record after verifying the evidence?'))return;const changes=json('dbqPaymentChanges');const v=await adminRequest('admin.dbq.execute',{operation:'payment_update',recordType:$('dbqPaymentRecordType').value,recordId:$('dbqPaymentRecordId').value,changes,confirmAmount:$('dbqPaymentConfirmAmount').checked,operator:$('dbqPaymentEditOperator').value||$('dbqOperator').value,verificationRef:$('dbqVerificationRef').value});$('dbqPayment').textContent=JSON.stringify(v,null,2);status('Payment record updated and verified.');void loadStudentPayments();}catch(e){status(e.message,false);}};const score=()=>({operation:'score_update',regno:$('dbqRegno').value,course:$('dbqCourse').value,session:$('dbqSession').value,semester:$('dbqSemester').value,ca:$('dbqCA').value,exam:$('dbqExam').value,operator:$('dbqOperator').value,modifier:$('dbqModifier').value,publish:$('dbqPublish').checked});$('dbqScoreUpdate').onclick=async()=>{if(!confirm('Update and verify this score row?'))return;status('Updating score...');try{const v=await adminRequest('admin.dbq.execute',score());status(`Score updated and verified. Affected rows: ${v.affected_rows??'reported by gateway'}.`);void loadStudent();}catch(e){status(e.message,false);}};$('dbqScorePublish').onclick=async()=>{if(!confirm('Publish this score row?'))return;const p=score();p.operation='score_publish';status('Publishing score...');try{await adminRequest('admin.dbq.execute',p);status('Score row published.');void loadStudent();}catch(e){status(e.message,false);}};const generic=async(operation,confirmed=false)=>{if(!table())return status('Select a table first.',false);try{const keys=json('dbqKeys');const values=json('dbqValues');if(!confirmed&&!confirm(`${operation} row in ${table()}?`))return;status(`${operation} row...`);const v=await adminRequest('admin.dbq.execute',{operation,table:table(),keys,values,confirmed});$('dbqGeneric').textContent=JSON.stringify(v,null,2);status(`${operation} completed. Affected rows: ${v.affected_rows??'reported by gateway'}.`);void loadRows();}catch(e){status(e.message,false);}};$('dbqPing').onclick=async()=>{status('Testing database gateway...');try{const v=await adminRequest('admin.dbq.ping',{operation:'dbq_ping'});status(`Database connection passed. ${JSON.stringify(v.result||'')}`);}catch(e){status(e.message,false);}};$('dbqInsert').onclick=()=>generic('insert',true);$('dbqUpdate').onclick=()=>generic('update',true);$('dbqDelete').onclick=()=>generic('delete',false);$('dbqLoadCatalog').onclick=loadCatalog;$('dbqLoadWorkspaces').onclick=loadWorkspaces;$('dbqBatchCheck').onclick=()=>diagnostics('batch_check');$('dbqMapCheck').onclick=()=>diagnostics('map_check');$('dbqLoadSchema').onclick=loadSchema;$('dbqLoadRows').onclick=()=>{dbqOffset=0;void loadRows();};$('dbqTable').onchange=()=>{dbqOffset=0;void loadSchema();void loadRows();};$('dbqPrevRows').onclick=()=>{dbqOffset=Math.max(0,dbqOffset-Math.max(1,Number($('dbqLimit').value)||50));void loadRows();};$('dbqNextRows').onclick=()=>{dbqOffset+=Math.max(1,Number($('dbqLimit').value)||50);void loadRows();};$('dbqStudentLookup').onclick=loadStudent;$('dbqStudentAccountLookup').onclick=loadStudentAccount;$('dbqStudentPasswordReset').onclick=resetStudentPassword;})();</script>"""


def _execution_admin_section() -> str:
    return """<section><h2>Execution backend</h2><p class='hint'>Only administrators can change where sandbox-compatible tasks and Telegram image OCR run. Novita remains the default. SSH secrets are never returned after saving; leave a secret blank to keep it.</p><label>Backend<select id='executionBackend'><option value='novita'>Novita Sandbox</option><option value='vps'>Linux VPS over SSH</option></select></label><label>VPS host<input id='vpsHost' placeholder='vps.example.com or IP address'></label><label>SSH port<input id='vpsPort' type='number' min='1' max='65535' value='22'></label><label>Linux username<input id='vpsUsername' placeholder='administrator'></label><label>Password<input id='vpsPassword' type='password' placeholder='Leave blank to keep current password'></label><label>Private key<textarea id='vpsPrivateKey' rows='6' autocomplete='off' spellcheck='false' placeholder='Paste the full multiline OpenSSH or PEM private key; leave blank to keep current key'></textarea></label><label>Host-key fingerprint<input id='vpsFingerprint' placeholder='SHA256:...'></label><label>Host-key policy<select id='vpsPolicy'><option value='fingerprint'>Require configured fingerprint</option><option value='accept_any'>Accept any host key (less secure)</option></select></label><label>Remote workspace<input id='vpsWorkspace' value='/workspace'></label><label>Connect timeout (seconds)<input id='vpsTimeout' type='number' min='1' max='60' value='15'></label><button id='loadExecution' class='secondary'>Load current settings</button><button id='testExecution'>Test SSH connection</button><button id='saveExecution'>Save execution settings</button><pre id='executionStatus' class='hint'>Secrets are shown only as configured/not configured.</pre></section><script>(()=>{const $=id=>document.getElementById(id);const status=(text,ok=true)=>{$('executionStatus').textContent=text;$('executionStatus').style.color=ok?'#a7f3d0':'#fca5a5';};const load=async()=>{const r=await fetch('/api/admin/execution-settings',{cache:'no-store'});if(!r.ok)throw new Error(`Load failed: ${r.status}`);const v=await r.json();const x=v.vps||{};$('executionBackend').value=v.backend||'novita';$('vpsHost').value=x.host||'';$('vpsPort').value=x.port||22;$('vpsUsername').value=x.username||'';$('vpsFingerprint').value=x.host_key_fingerprint||'';$('vpsPolicy').value=x.host_key_policy||'fingerprint';$('vpsWorkspace').value=x.workspace_dir||'/workspace';$('vpsTimeout').value=x.connect_timeout||15;$('vpsPassword').value='';$('vpsPrivateKey').value='';status(`Loaded. Password configured: ${!!x.passwordConfigured}; private key configured: ${!!x.privateKeyConfigured}.`);};const adminRequest=(action,payload)=>{if(typeof window.nanobotAdminRequest!=='function')throw new Error('Admin connection is not ready');return window.nanobotAdminRequest(action,payload);};const fields=()=>({backend:$('executionBackend').value,host:$('vpsHost').value,port:$('vpsPort').value,username:$('vpsUsername').value,password:$('vpsPassword').value,privateKey:$('vpsPrivateKey').value,hostKeyFingerprint:$('vpsFingerprint').value,hostKeyPolicy:$('vpsPolicy').value,workspaceDir:$('vpsWorkspace').value,connectTimeout:$('vpsTimeout').value});$('loadExecution').onclick=()=>load().catch(e=>status(e.message,false));$('saveExecution').onclick=async()=>{status('Saving...');try{const v=await adminRequest('admin.execution.save',fields());$('vpsPassword').value='';$('vpsPrivateKey').value='';status(`Saved. Active backend: ${v.backend}`);}catch(e){status(e.message,false);}};$('testExecution').onclick=async()=>{status('Testing SSH connection...');try{const v=await adminRequest('admin.execution.test',fields());status(`Connection passed. Platform: ${v.platform||'Novita selected'}; fingerprint: ${v.host_key_fingerprint||'not applicable'}`);}catch(e){status(e.message,false);}};void load().catch(e=>status(e.message,false));})();</script>"""


def _admin_page(rows: list[dict[str, Any]]) -> str:
    body_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('username') or ''))}</td>"
        f"<td>{html.escape(str(row.get('first_name') or ''))} {html.escape(str(row.get('last_name') or ''))}</td>"
        f"<td>{html.escape(str(row.get('sender_id') or ''))}</td>"
        f"<td>{html.escape(str(row.get('last_seen') or ''))}</td>"
        f"<td>{html.escape(str(row.get('message_count') or 0))}</td>"
        "</tr>"
        for row in rows
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Nanobot Admin</title><style>body{{font-family:system-ui,sans-serif;background:#0b1020;color:#eef2ff;margin:2rem;max-width:1100px}}section{{background:#121a31;border:1px solid #2a3557;border-radius:12px;padding:1.2rem;margin:1rem 0}}table{{border-collapse:collapse;width:100%;background:#121a31}}th,td{{padding:.7rem;border:1px solid #2a3557;text-align:left}}th{{color:#93c5fd}}input,select,textarea{{box-sizing:border-box;width:100%;padding:.65rem;border-radius:7px;border:1px solid #46557e;background:#0b1020;color:#eef2ff;margin:.25rem 0 .7rem}}button{{padding:.65rem .9rem;border:0;border-radius:7px;background:#2563eb;color:white;cursor:pointer;margin:.25rem .4rem .25rem 0}}button.secondary{{background:#334155}}#status{{min-height:1.4rem;color:#a7f3d0;white-space:pre-wrap}}.hint{{color:#aab6d3;font-size:.9rem}}code{{color:#a7f3d0}}@media (max-width:680px){{body{{margin:.5rem;max-width:none}}section{{padding:.75rem;border-radius:8px}}button{{width:100%;margin:.25rem 0}}label{{display:block}}table{{font-size:.78rem;min-width:680px}}#dbqRowsView,#dbqSchemaView{{-webkit-overflow-scrolling:touch}}pre{{font-size:.78rem;max-height:20rem;overflow:auto;white-space:pre-wrap;word-break:break-word}}}}</style></head><body><h1>Nanobot Admin</h1><section><h2>Provider settings</h2><p class='hint'>Update the OpenAI-compatible API base URL, API key, and model ID. The API key is never displayed after saving.</p><label>API base URL<input id='apiBase' type='url' placeholder='https://example.com/v1'></label><label>API key<input id='apiKey' type='password' placeholder='Leave blank to keep the current key'></label><label>Model ID<input id='model' list='modelList' placeholder='gemini-3.1-flash-lite'><datalist id='modelList'></datalist></label><button id='loadModels' class='secondary'>Load models</button><button id='testProvider'>Test connection</button><button id='saveProvider'>Save settings</button><p id='status'></p></section>{_execution_admin_section()}{_dbq_admin_section()}<section><h2>Supabase users, credits and payments</h2><p class='hint'>This view reads the existing Supabase <code>profiles</code>, <code>telegram_accounts</code>, and <code>payment_claims</code> tables. Credit changes use the database-backed ledger path.</p><button id='refreshSupabase' class='secondary'>Load database users</button><div style='overflow:auto;margin-top:1rem'><table><thead><tr><th>User ID</th><th>Name / email</th><th>Role</th><th>Status</th><th>Credits</th><th>Last seen</th><th>Questions</th><th>Telegram</th></tr></thead><tbody id='supabaseRows'><tr><td colspan='8'>Click Load database users.</td></tr></tbody></table></div><label>Selected user ID<input id='supabaseUserId' placeholder='UUID from the table'></label><label>Grant credits<input id='grantAmount' type='number' min='1' max='1000000' value='1000'></label><label><input id='blockState' type='checkbox'> Block selected user</label><button id='grantCredits'>Grant credits</button><button id='blockUser' class='secondary'>Block / unblock selected user</button><button id='deleteUser' class='secondary'>Delete selected user</button><h3>Announcement</h3><label>Title<input id='announcementTitle' value='Nanobot announcement'></label><label>Message<textarea id='announcementMessage' rows='3' placeholder='Message shown to users'></textarea></label><button id='sendAnnouncement'>Publish announcement</button><h3>Payment claims</h3><button id='loadPayments' class='secondary'>Load payment claims</button><pre id='paymentRows' class='hint'>No payment claims loaded.</pre></section><section><h2>Telegram user questions</h2><p class='hint'>Recent task instructions captured from Telegram. Credentials and token-like values are redacted before storage.</p><button id='loadQuestions' class='secondary'>Load question history</button><pre id='questionRows' class='hint'>No question history loaded.</pre></section><section><h2>WebUI user questions</h2><p class='hint'>Questions asked by users on the WebUI website, with their name/email and the type/category of each question.</p><button id='loadWebuiQuestions' class='secondary'>Load WebUI question history</button><pre id='webuiQuestionRows' class='hint'>No WebUI question history loaded.</pre></section><section><h2>Telegram users</h2><p>Telegram users recorded: <strong>{len(rows)}</strong></p><table><thead><tr><th>Username</th><th>Name</th><th>Telegram ID</th><th>Last seen</th><th>Messages</th></tr></thead><tbody>{body_rows or '<tr><td colspan="5">No users recorded yet.</td></tr>'}</tbody></table><p class='hint'><code>GET /api/admin/users</code> is available with the same Basic Auth credentials.</p></section><script>(()=>{{const $=id=>document.getElementById(id);const status=(text,ok=true)=>{{$('status').textContent=text;$('status').style.color=ok?'#a7f3d0':'#fca5a5';}};let socket=null;const pending=new Map();const request=(action,payload={{}})=>new Promise(async(resolve,reject)=>{{try{{if(!socket||socket.readyState!==1){{const boot=await fetch('/api/admin/ws-bootstrap',{{cache:'no-store'}}).then(r=>r.ok?r.json():Promise.reject(new Error('Admin session expired')));const scheme=location.protocol==='https:'?'wss':'ws';socket=new WebSocket(`${{scheme}}://${{location.host}}${{boot.ws_path}}?token=${{encodeURIComponent(boot.token)}}&client_id=admin`);socket.onmessage=e=>{{const msg=JSON.parse(e.data);if(msg.event==='webui_response'&&pending.has(msg.request_id)){{const p=pending.get(msg.request_id);pending.delete(msg.request_id);msg.ok?p.resolve(msg.result):p.reject(new Error(msg.error?.message||'Admin request failed'));}}}};socket.onerror=()=>{{for(const p of pending.values())p.reject(new Error('Admin socket failed'));pending.clear();}};await new Promise((res,rej)=>{{socket.addEventListener('open',res,{{once:true}});socket.addEventListener('error',()=>rej(new Error('Admin socket failed')),{{once:true}});}});}}const requestId=`admin-${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;pending.set(requestId,{{resolve,reject}});socket.send(JSON.stringify({{type:'webui_request',request_id:requestId,action,payload}}));}}catch(e){{reject(e);}}}});window.nanobotAdminRequest=request;const fields=()=>({{apiBase:$('apiBase').value,apiKey:$('apiKey').value,model:$('model').value}});fetch('/api/admin/provider-settings',{{cache:'no-store'}}).then(r=>r.json()).then(v=>{{$('apiBase').value=v.apiBase||'';$('model').value=v.model||'';}}).catch(e=>status(e.message,false));$('loadModels').onclick=async()=>{{status('Loading models...');try{{const v=await request('admin.provider.models',fields());const list=$('modelList');list.replaceChildren(...(v.models||[]).map(id=>{{const o=document.createElement('option');o.value=id;return o}}));status(`Loaded ${{v.count||0}} model(s). Choose one and save.`);}}catch(e){{status(e.message,false);}}}};$('testProvider').onclick=async()=>{{status('Testing provider...');try{{const v=await request('admin.provider.test',fields());status(`Provider test passed. Response: ${{v.response||'(empty)'}}`);}}catch(e){{status(e.message,false);}}}};$('saveProvider').onclick=async()=>{{status('Saving settings...');try{{const v=await request('admin.provider.save',fields());$('apiKey').value='';status(`Saved. Active model: ${{v.model||$('model').value}}`);}}catch(e){{status(e.message,false);}}}};const adminAction=async(kind,extra={{}})=>{{const userId=$('supabaseUserId').value.trim();if(kind!=='announcement'&&!userId){{status('Select or enter a user ID first.',false);return;}}status('Working...');try{{const v=await request('admin.supabase.action',{{kind,userId,...extra}});if(kind==='announcement'){{status(`Announcement delivered to ${{v.sent||0}}/${{v.total||0}} Telegram chat(s); failed: ${{v.failed||0}}.`);}}else{{status(`Completed: ${{v.action||kind}}`);}}$('refreshSupabase').click();}}catch(e){{status(e.message,false);}}}};$('refreshSupabase').onclick=async()=>{{status('Loading Supabase users...');try{{const r=await fetch('/api/admin/supabase/users',{{cache:'no-store'}});if(!r.ok)throw new Error(`Database users request failed: ${{r.status}}`);const v=await r.json();const rows=v.users||[];$('supabaseRows').replaceChildren(...rows.map(u=>{{const tr=document.createElement('tr');tr.dataset.id=String(u.id||'');const tg=(u.telegram_accounts||[]).map(a=>`@${{a.username||''}} (${{a.telegram_user_id||''}})`).join(', ');tr.innerHTML=`<td>${{String(u.id||'')}}</td><td>${{String(u.name||'')}}<br><span class='hint'>${{String(u.email||'')}}</span></td><td>${{String(u.role||'')}}</td><td>${{String(u.status||'')}}</td><td>${{String(u.total_credits||0)}}</td><td>${{u.last_seen_at?String(u.last_seen_at).slice(0,16).replace('T',' '):'—'}}</td><td>${{String(u.questions_count??0)}}</td><td>${{tg||'—'}}</td>`;tr.onclick=()=>{{$('supabaseUserId').value=String(u.id||'');document.querySelectorAll('#supabaseRows tr').forEach(x=>x.style.outline='');tr.style.outline='2px solid #60a5fa';}};return tr}}));status(`Loaded ${{rows.length}} Supabase user(s). Click a row to select it.`);}}catch(e){{status(e.message,false);}}}};$('grantCredits').onclick=()=>adminAction('grant',{{amount:Number($('grantAmount').value||0)}});$('blockUser').onclick=()=>adminAction('block',{{blocked:$('blockState').checked}});$('deleteUser').onclick=()=>{{if(confirm('Delete the selected Supabase user and their Auth account? This cannot be undone.'))void adminAction('delete')}};$('sendAnnouncement').onclick=()=>adminAction('announcement',{{title:$('announcementTitle').value,message:$('announcementMessage').value}});$('loadPayments').onclick=async()=>{{status('Loading payment claims...');try{{const r=await fetch('/api/admin/supabase/payments',{{cache:'no-store'}});if(!r.ok)throw new Error(`Payment claims request failed: ${{r.status}}`);const v=await r.json();$('paymentRows').textContent=JSON.stringify(v.payments||[],null,2);status(`Loaded ${{(v.payments||[]).length}} payment claim(s).`);}}catch(e){{status(e.message,false);}}}};$('loadQuestions').onclick=async()=>{{status('Loading Telegram question history...');try{{const r=await fetch('/api/admin/supabase/questions',{{cache:'no-store'}});if(!r.ok)throw new Error(`Question history request failed: ${{r.status}}`);const v=await r.json();$('questionRows').textContent=JSON.stringify(v.questions||[],null,2);status(`Loaded ${{(v.questions||[]).length}} Telegram question(s).`);}}catch(e){{status(e.message,false);}}}};$('loadWebuiQuestions').onclick=async()=>{{status('Loading WebUI question history...');try{{const r=await fetch('/api/admin/supabase/webui-questions',{{cache:'no-store'}});if(!r.ok)throw new Error(`WebUI question history request failed: ${{r.status}}`);const v=await r.json();$('webuiQuestionRows').textContent=JSON.stringify((v.questions||[]).map(q=>({{user:q.user_name||q.user_id||'?',email:q.user_email||'',type:q.category||'',question:(q.message||'').slice(0,300),at:q.created_at}})),null,2);status(`Loaded ${{(v.questions||[]).length}} WebUI question(s).`);}}catch(e){{status(e.message,false);}}}};void $('refreshSupabase').click();void $('loadQuestions').click();}})();</script></body></html>"""


def admin_route(
    request: Any,
    path: str,
    *,
    issue_admin_token: Callable[[], str] | None = None,
    ws_path: str = "/",
    refresh_runtime_config: Callable[[], Any] | None = None,
) -> Response | None:
    """Return an admin response for dashboard, settings, or registry routes."""
    admin_paths = {
        "/admin",
        "/api/admin/users",
        "/api/admin/provider-settings",
        "/api/admin/provider-models",
        "/api/admin/provider-test",
        "/api/admin/provider-settings/save",
        "/api/admin/execution-settings",
        "/api/admin/execution-test",
        "/api/admin/dbq/catalog",
        "/api/admin/dbq/workspaces",
        "/api/admin/dbq/person-search",
        "/api/admin/dbq/table-search",
        "/api/admin/dbq/schema",
        "/api/admin/dbq/read",
        "/api/admin/dbq/student",
        "/api/admin/dbq/student-payments",
        "/api/admin/dbq/batch-check",
        "/api/admin/dbq/map-check",
        "/api/admin/dbq/status",
        "/api/admin/dbq/action",
        "/api/admin/ws-bootstrap",
        "/api/admin/supabase/users",
        "/api/admin/supabase/payments",
        "/api/admin/supabase/questions",
        "/api/admin/supabase/webui-questions",
        "/api/admin/supabase/announcements",
        "/api/admin/supabase/action",
    }
    if path not in admin_paths:
        return None
    if not _authorized(request):
        return _auth_error()
    if path == "/api/admin/ws-bootstrap":
        if issue_admin_token is None:
            return http_error(503, "Admin WebSocket bootstrap is unavailable")
        return http_json_response({"token": issue_admin_token(), "ws_path": ws_path})
    payload = _payload(request)
    if path == "/api/admin/provider-settings":
        return http_json_response(_provider_settings())
    if path == "/api/admin/execution-settings":
        is_mutation = isinstance(
            getattr(request, "_nanobot_webui_mutation_payload", None),
            dict,
        ) or str(getattr(request, "method", "GET")).upper() == "POST"
        if is_mutation:
            return _save_execution_settings(payload, refresh_runtime_config=refresh_runtime_config)
        return http_json_response(_execution_settings())
    if path == "/api/admin/execution-test":
        return _test_execution_response(payload)
    if path == "/api/admin/dbq/catalog":
        try:
            return http_json_response(dbq_admin.catalog(_text(payload, "search", maximum=128)))
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/workspaces":
        try:
            return http_json_response(dbq_admin.portal_workspaces(_text(payload, "search", maximum=128)))
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/person-search":
        try:
            return http_json_response(dbq_admin.person_search(
                _text(payload, "term", maximum=128),
                _text(payload, "scope", maximum=16) or "all",
                _text(payload, "limit", maximum=4) or "50",
            ))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/table-search":
        try:
            return http_json_response(dbq_admin.table_search(
                _text(payload, "table", maximum=64),
                _text(payload, "term", maximum=128),
                _text(payload, "limit", maximum=4) or "50",
            ))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/schema":
        try:
            return http_json_response(dbq_admin.schema(_text(payload, "table", maximum=64)))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/read":
        try:
            return http_json_response(dbq_admin.read_table(
                _text(payload, "table", maximum=64),
                _text(payload, "limit", maximum=4) or "50",
                _text(payload, "offset", maximum=6) or "0",
            ))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/student":
        try:
            return http_json_response(dbq_admin.student_lookup(
                _text(payload, "regno", maximum=128),
                _text(payload, "session", maximum=32),
            ))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/student-payments":
        try:
            return http_json_response(dbq_admin.student_payments({
                "regno": _text(payload, "regno", maximum=128),
                "session": _text(payload, "session", maximum=32),
                "semester": _text(payload, "semester", maximum=8),
            }))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/batch-check":
        try:
            return http_json_response(dbq_admin.batch_check({
                "course": _text(payload, "course", maximum=64),
                "session": _text(payload, "session", maximum=32),
                "semester": _text(payload, "semester", maximum=8),
            }))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/map-check":
        try:
            return http_json_response(dbq_admin.map_check({
                "course": _text(payload, "course", maximum=64),
                "session": _text(payload, "session", maximum=32),
                "semester": _text(payload, "semester", maximum=8),
                "prog": _text(payload, "prog", maximum=64) or "ELC",
                "portalCategory": _text(payload, "portalCategory", maximum=32) or "ug",
            }))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/status":
        try:
            return http_json_response(dbq_admin.ping())
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/dbq/action":
        try:
            return http_json_response(dbq_admin.execute_action(payload))
        except dbq_admin.DBQValidationError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=400)
        except dbq_admin.DBQError as exc:
            return http_json_response({"ok": False, "error": str(exc)}, status=502)
    if path == "/api/admin/supabase/users":
        try:
            return http_json_response({"ok": True, "users": supabase_admin.users()})
        except supabase_admin.SupabaseAdminError as exc:
            return http_error(502, str(exc))
    if path == "/api/admin/supabase/payments":
        try:
            return http_json_response({"ok": True, "payments": supabase_admin.payment_claims()})
        except supabase_admin.SupabaseAdminError as exc:
            return http_error(502, str(exc))
    if path == "/api/admin/supabase/questions":
        try:
            return http_json_response({"ok": True, "questions": supabase_admin.telegram_question_history()})
        except supabase_admin.SupabaseAdminError as exc:
            return http_error(502, str(exc))
    if path == "/api/admin/supabase/webui-questions":
        try:
            return http_json_response({"ok": True, "questions": supabase_admin.webui_question_history()})
        except supabase_admin.SupabaseAdminError as exc:
            return http_error(502, str(exc))
    if path == "/api/admin/supabase/announcements":
        try:
            return http_json_response({"ok": True, "announcements": supabase_admin.announcements()})
        except supabase_admin.SupabaseAdminError as exc:
            return http_error(502, str(exc))
    if path == "/api/admin/supabase/action":
        try:
            kind = _text(payload, "kind", maximum=40)
            if kind == "announcement":
                return http_json_response(supabase_admin.announcement(payload.get("title"), payload.get("message")))
            return http_json_response(supabase_admin.user_action(
                kind,
                payload.get("userId"),
                amount=payload.get("amount", 0),
                blocked=payload.get("blocked", False),
            ))
        except supabase_admin.SupabaseAdminError as exc:
            return http_error(400, str(exc))

    if path == "/api/admin/provider-models":
        return _models_response(payload)
    if path == "/api/admin/provider-test":
        return _test_response(payload)
    if path == "/api/admin/provider-settings/save":
        return _save_response(payload, refresh_runtime_config=refresh_runtime_config)
    rows = list_users()
    return http_response(
        _admin_page(rows).encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )
