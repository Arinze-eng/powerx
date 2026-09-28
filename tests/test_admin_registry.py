import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx

import nanobot.admin_registry as admin_registry
from nanobot.webui.gateway_tokens import GatewayTokenStore


def _request(path: str = "/admin", *, password: str = "nethunter") -> SimpleNamespace:
    encoded = base64.b64encode(f"admin:{password}".encode()).decode()
    return SimpleNamespace(
        path=path,
        headers={"Authorization": f"Basic {encoded}"},
    )


def test_admin_password_auth_and_dashboard(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    unauthorized = admin_registry.admin_route(SimpleNamespace(path="/admin", headers={}), "/admin")
    assert unauthorized is not None
    assert unauthorized.status_code == 401

    response = admin_registry.admin_route(_request(), "/admin")
    assert response is not None
    assert response.status_code == 200
    assert admin_registry.admin_route(_request(password="nethunter"), "/admin") is not None

    blank_username = SimpleNamespace(
        path="/admin",
        headers={"Authorization": "Basic " + base64.b64encode(b":nethunter").decode()},
    )
    assert admin_registry.admin_route(blank_username, "/admin") is not None
    body = bytes(response.body).decode()
    assert "Provider settings" in body
    assert "dbqStudentPasswordReset" in body
    assert "Student account access" in body
    assert "Password values are never displayed" in body
    assert "Load models" in body
    assert "Save settings" in body


def test_admin_question_history_is_protected_and_rendered(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(
        admin_registry.supabase_admin,
        "telegram_question_history",
        lambda: [{"telegram_user_id": 42, "question": "summarize the uploaded file"}],
    )

    unauthorized = admin_registry.admin_route(
        SimpleNamespace(path="/api/admin/supabase/questions", headers={}),
        "/api/admin/supabase/questions",
    )
    assert unauthorized is not None
    assert unauthorized.status_code == 401

    response = admin_registry.admin_route(
        _request("/api/admin/supabase/questions"),
        "/api/admin/supabase/questions",
    )
    assert response is not None
    assert response.status_code == 200
    assert "summarize the uploaded file" in bytes(response.body).decode()

    dashboard = admin_registry.admin_route(_request(), "/admin")
    assert dashboard is not None
    assert "Telegram user questions" in bytes(dashboard.body).decode()
    assert "Load question history" in bytes(dashboard.body).decode()


def test_admin_provider_settings_save_persists_and_refreshes(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    refreshed = []

    request = _request("/api/admin/provider-settings/save")
    request._nanobot_webui_mutation_payload = {
        "apiBase": "https://example.test/v1",
        "apiKey": "secret-value",
        "model": "example-model",
    }
    response = admin_registry.admin_route(
        request,
        "/api/admin/provider-settings/save",
        refresh_runtime_config=lambda: refreshed.append(True),
    )

    assert response is not None
    assert response.status_code == 200
    assert refreshed == [True]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"]["custom"]["apiBase"] == "https://example.test/v1"
    assert saved["providers"]["custom"]["apiKey"] == "secret-value"
    assert saved["agents"]["defaults"]["model"] == "custom/example-model"
    assert "SUPABASE_TOKEN_ENCRYPTION_KEY" not in json.dumps(saved)


def test_admin_provider_settings_never_returns_api_key(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    monkeypatch.setenv("LLM_API_KEY", "secret-not-for-response")

    response = admin_registry.admin_route(
        _request("/api/admin/provider-settings"),
        "/api/admin/provider-settings",
    )

    assert response is not None
    body = bytes(response.body).decode()
    assert "apiKeyConfigured" in body
    assert '"apiKey":' not in body
    assert "secret-not-for-response" not in body


def test_admin_token_is_consumed_as_admin_audience() -> None:
    store = GatewayTokenStore()
    token = store.issue_token(60, audience="admin")
    assert store.take_issued_token_audience(token) == "admin"


def test_provider_models_retries_with_x_api_key_after_bearer_401(monkeypatch) -> None:
    requests: list[dict[str, str]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, authorized: bool) -> None:
            self.status_code = 200 if authorized else 401

        def raise_for_status(self) -> None:
            if self.status_code == 401:
                request = httpx.Request("GET", "https://provider.example.test/models")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {"data": [{"id": "model-a"}]}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url: str, *, headers: dict[str, str]) -> FakeResponse:
            requests.append(headers)
            return FakeResponse("x-api-key" in headers)

    monkeypatch.setattr(admin_registry.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        admin_registry,
        "_credentials",
        lambda _payload: ("https://provider.example.test", "model-a", "test-key"),
    )

    response = admin_registry._models_response({})

    assert response.status_code == 200
    assert json.loads(bytes(response.body).decode())["models"] == ["model-a"]
    assert requests == [
        {"Accept": "application/json", "Authorization": "Bearer test-key"},
        {"Accept": "application/json", "x-api-key": "test-key"},
    ]


def test_execution_settings_are_admin_only_redacted_and_persisted(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    refreshed = []

    unauthorized = admin_registry.admin_route(
        SimpleNamespace(path="/api/admin/execution-settings", headers={}),
        "/api/admin/execution-settings",
    )
    assert unauthorized is not None
    assert unauthorized.status_code == 401

    request = _request("/api/admin/execution-settings")
    request._nanobot_webui_mutation_payload = {
        "backend": "vps",
        "host": "vps.example.test",
        "port": "2222",
        "username": "administrator",
        "password": "test-password",
        "hostKeyFingerprint": "",
        "hostKeyPolicy": "accept_any",
        "workspaceDir": "/workspace",
        "connectTimeout": "20",
    }
    response = admin_registry.admin_route(
        request,
        "/api/admin/execution-settings",
        refresh_runtime_config=lambda: refreshed.append(True),
    )
    assert response is not None
    assert response.status_code == 200
    assert refreshed == [True]
    body = bytes(response.body).decode()
    assert "test-password" not in body
    assert '"passwordConfigured": true' in body
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["execution"]["backend"] == "vps"
    assert saved["execution"]["vps"]["password"] == "test-password"

    blank_secret_request = _request("/api/admin/execution-settings")
    blank_secret_request._nanobot_webui_mutation_payload = {
        "backend": "vps",
        "host": "vps.example.test",
        "port": "2222",
        "username": "administrator",
        "password": "",
        "privateKey": "",
        "hostKeyFingerprint": "",
        "hostKeyPolicy": "accept_any",
        "workspaceDir": "/workspace",
        "connectTimeout": "20",
    }
    retained = admin_registry.admin_route(
        blank_secret_request,
        "/api/admin/execution-settings",
    )
    assert retained is not None
    assert retained.status_code == 200
    saved_again = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_again["execution"]["vps"]["password"] == "test-password"

    invalid_request = _request("/api/admin/execution-settings")
    invalid_request._nanobot_webui_mutation_payload = {
        "backend": "vps",
        "host": "https://not-a-host.example",
        "port": "2222",
        "username": "administrator",
        "password": "test-password",
        "hostKeyPolicy": "accept_any",
    }
    invalid = admin_registry.admin_route(invalid_request, "/api/admin/execution-settings")
    assert invalid is not None
    assert invalid.status_code == 400


def test_admin_can_switch_saved_vps_configuration_back_to_novita(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setenv("NANOBOT_EXECUTION_BACKEND", "vps")
    monkeypatch.setenv("NANOBOT_VPS_HOST", "vps.example.test")
    monkeypatch.setenv("NANOBOT_VPS_PORT", "10050")
    monkeypatch.setenv("NANOBOT_VPS_USERNAME", "administrator")
    monkeypatch.setenv("NANOBOT_VPS_PASSWORD", "fixture-secret")
    monkeypatch.setenv("NANOBOT_VPS_HOST_KEY_POLICY", "accept_any")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)

    switch_to_novita = _request("/api/admin/execution-settings")
    switch_to_novita._nanobot_webui_mutation_payload = {
        "backend": "novita",
        "host": "",
        "port": "10050",
        "username": "",
        "password": "",
        "privateKey": "",
        "hostKeyFingerprint": "",
        "hostKeyPolicy": "accept_any",
        "workspaceDir": "/workspace",
        "connectTimeout": "15",
    }
    response = admin_registry.admin_route(switch_to_novita, "/api/admin/execution-settings")

    assert response is not None
    assert response.status_code == 200
    body = bytes(response.body).decode()
    assert '"backend": "novita"' in body
    assert "fixture-secret" not in body
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["execution"]["backend"] == "novita"

    settings = admin_registry.admin_route(
        _request("/api/admin/execution-settings"),
        "/api/admin/execution-settings",
    )
    assert settings is not None
    assert settings.status_code == 200
    assert '"backend": "novita"' in bytes(settings.body).decode()

    execution_test = admin_registry.admin_route(
        _request("/api/admin/execution-test"),
        "/api/admin/execution-test",
    )
    assert execution_test is not None
    assert execution_test.status_code == 200
    assert '"backend": "novita"' in bytes(execution_test.body).decode()


def test_execution_test_returns_novita_status_without_ssh(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    response = admin_registry.admin_route(
        _request("/api/admin/execution-test"),
        "/api/admin/execution-test",
    )
    assert response is not None
    assert response.status_code == 200
    body = bytes(response.body).decode()
    assert '"backend": "novita"' in body
    assert "password" not in body.lower()


def test_execution_test_uses_current_form_values_and_returns_platform(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    captured = {}

    def fake_test(config):
        captured["config"] = config
        return {
            "ok": True,
            "platform": "Linux",
            "host_key_fingerprint": "SHA256:testfingerprint",
        }

    monkeypatch.setattr(admin_registry, "_run_vps_test", fake_test)
    request = _request("/api/admin/execution-test")
    request._nanobot_webui_mutation_payload = {
        "backend": "vps",
        "host": "vps.example.test",
        "port": "10050",
        "username": "administrator",
        "password": "test-password",
        "hostKeyPolicy": "accept_any",
        "workspaceDir": "/workspace",
        "connectTimeout": "15",
    }
    response = admin_registry.admin_route(request, "/api/admin/execution-test")
    assert response is not None
    assert response.status_code == 200
    assert '"platform": "Linux"' in bytes(response.body).decode()
    assert captured["config"].host == "vps.example.test"
    assert captured["config"].port == 10050
    assert captured["config"].host_key_policy == "accept_any"
    assert captured["config"].password == "test-password"


def test_execution_test_returns_connection_diagnostic(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    monkeypatch.setattr(
        admin_registry,
        "_run_vps_test",
        lambda _config: (_ for _ in ()).throw(RuntimeError("ConnectionRefusedError: [Errno 111] connection refused")),
    )
    request = _request("/api/admin/execution-test")
    request._nanobot_webui_mutation_payload = {
        "backend": "vps",
        "host": "vps.example.test",
        "port": "10050",
        "username": "administrator",
        "password": "test-password",
        "hostKeyPolicy": "accept_any",
        "workspaceDir": "/workspace",
        "connectTimeout": "15",
    }
    response = admin_registry.admin_route(request, "/api/admin/execution-test")
    assert response is not None
    assert response.status_code == 502
    body = bytes(response.body).decode()
    assert "connection refused" in body
    assert "test-password" not in body


def test_dbq_admin_workspace_is_protected_and_rendered(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    unauthorized = admin_registry.admin_route(
        SimpleNamespace(path="/api/admin/dbq/catalog", headers={}),
        "/api/admin/dbq/catalog",
    )
    assert unauthorized is not None
    assert unauthorized.status_code == 401

    monkeypatch.setattr(admin_registry.dbq_admin, "catalog", lambda search: {
        "database": "anuoluwatide9db",
        "table_count": 208,
        "tables": [{"TABLE_NAME": "studenttb", "TABLE_TYPE": "BASE TABLE", "TABLE_ROWS": 4}],
    })
    response = admin_registry.admin_route(
        _request("/api/admin/dbq/catalog"),
        "/api/admin/dbq/catalog",
    )
    assert response is not None
    assert response.status_code == 200
    assert '"table_count": 208' in bytes(response.body).decode()

    dashboard = admin_registry.admin_route(_request(), "/admin")
    assert dashboard is not None
    body = bytes(dashboard.body).decode()
    assert "University database workspace" in body
    assert "Load all tables" in body
    assert "admin.dbq.execute" in body


def test_dbq_mutation_route_delegates_to_controlled_operation(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    captured = {}

    def fake_execute(payload):
        captured.update(payload)
        return {"ok": True, "operation": "score_update", "affected_rows": 1}

    monkeypatch.setattr(admin_registry.dbq_admin, "execute_action", fake_execute)
    request = _request("/api/admin/dbq/action")
    request._nanobot_webui_mutation_payload = {
        "operation": "score_update",
        "regno": "22/205EEE/132",
        "course": "FEG412",
        "session": "2025/2026",
        "semester": "1st",
        "ca": "20",
        "exam": "20",
        "operator": "ACA2538",
        "modifier": "ACA_ADMIN",
    }
    response = admin_registry.admin_route(request, "/api/admin/dbq/action")
    assert response is not None
    assert response.status_code == 200
    assert captured["operation"] == "score_update"


def test_dbq_gateway_errors_are_json(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(
        admin_registry.dbq_admin,
        "catalog",
        lambda _search: (_ for _ in ()).throw(admin_registry.dbq_admin.DBQError("gateway unavailable")),
    )
    response = admin_registry.admin_route(_request("/api/admin/dbq/catalog"), "/api/admin/dbq/catalog")
    assert response is not None
    assert response.status_code == 502
    body = json.loads(bytes(response.body).decode())
    assert body["ok"] is False
    assert body["error"] == "gateway unavailable"


def test_dbq_student_payment_and_diagnostic_routes_are_admin_only(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry.dbq_admin, "student_payments", lambda payload: {"student": {"regno": payload["regno"]}, "payments": [], "details": []})
    monkeypatch.setattr(admin_registry.dbq_admin, "batch_check", lambda payload: {"row_counts": [], "unpublished": [], "upload_files": [], "allocations": []})
    monkeypatch.setattr(admin_registry.dbq_admin, "map_check", lambda payload: {"master_course": [], "session_curriculum": [], "allocations": []})
    for path in ("/api/admin/dbq/student-payments", "/api/admin/dbq/batch-check", "/api/admin/dbq/map-check"):
        denied = admin_registry.admin_route(SimpleNamespace(path=path, headers={}), path)
        assert denied is not None and denied.status_code == 401

    payment_request = _request("/api/admin/dbq/student-payments?regno=S1&session=2025%2F2026&semester=1st")
    response = admin_registry.admin_route(payment_request, payment_request.path.split("?", 1)[0])
    assert response is not None and response.status_code == 200
    assert json.loads(bytes(response.body).decode())["student"]["regno"] == "S1"


def test_dbq_dashboard_contains_payment_edit_and_full_mapping_controls(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    response = admin_registry.admin_route(_request(), "/admin")
    assert response is not None
    body = bytes(response.body).decode()
    for marker in (
        "View student payments", "Update payment record", "Main payment", "Payment detail",
        "Batch-check course results", "Check course map", "Write guidance", "admin.dbq.execute",
        "textarea id='vpsPrivateKey'", "multiline OpenSSH or PEM private key",
    ):
        assert marker in body


def test_admin_apk_user_activity_is_protected_and_rendered(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(
        admin_registry.supabase_admin,
        "apk_user_activity",
        lambda: [
            {
                "id": "u1",
                "name": "Ada",
                "email": "ada@example.com",
                "last_seen_at": "2026-09-15T09:30:00+00:00",
                "questions_count": 2,
                "questions": [
                    {
                        "message": "hello",
                        "category": "apk",
                        "created_at": "2026-09-15T09:30:00+00:00",
                    }
                ],
            }
        ],
    )

    unauthorized = admin_registry.admin_route(
        SimpleNamespace(path="/api/admin/supabase/apk-users", headers={}),
        "/api/admin/supabase/apk-users",
    )
    assert unauthorized is not None
    assert unauthorized.status_code == 401

    response = admin_registry.admin_route(
        _request(path="/api/admin/supabase/apk-users"),
        "/api/admin/supabase/apk-users",
    )
    assert response is not None
    assert response.status_code == 200
    payload = json.loads(bytes(response.body).decode())
    assert payload["ok"] is True
    assert payload["users"][0]["name"] == "Ada"
    assert payload["users"][0]["questions"][0]["category"] == "apk"

    dashboard = admin_registry.admin_route(_request(), "/admin")
    assert dashboard is not None
    body = bytes(dashboard.body).decode()
    assert "APK users" in body
    assert "loadApkUsers" in body
    assert "apkRows" in body


def test_apk_user_activity_groups_questions_per_user(monkeypatch) -> None:
    import nanobot.supabase_admin as sa

    def fake_request(method, path, **kwargs):
        if path.endswith("/profiles"):
            return [
                {
                    "id": "u1",
                    "name": "Ada",
                    "email": "a@e.com",
                    "last_seen_at": "2026-09-15T09:30:00+00:00",
                    "questions_count": 1,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        return [
            {
                "id": "q1",
                "user_id": "u1",
                "message": "hi",
                "category": "apk",
                "created_at": "2026-09-15T09:30:00+00:00",
            }
        ]

    monkeypatch.setattr(sa, "_request", fake_request)
    users = sa.apk_user_activity()
    assert len(users) == 1
    assert users[0]["questions"][0]["message"] == "hi"
    assert users[0]["questions"][0]["category"] == "apk"


def test_execution_admin_section_offers_the_tenki_backend(monkeypatch) -> None:
    """Tenki must be selectable in the admin panel, with its own settings form."""
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    response = admin_registry.admin_route(_request(), "/admin")
    assert response is not None
    body = bytes(response.body).decode()
    # The option an administrator actually picks from.
    assert "<option value='tenki'>Tenki Sandbox</option>" in body
    # Every setting the backend reads.
    for element in (
        "tenkiApiKey",
        "tenkiApiUrl",
        "tenkiSnapshotId",
        "tenkiImage",
        "tenkiCpuCores",
        "tenkiMemoryMb",
        "tenkiDiskSizeGb",
        "tenkiMaxDurationSeconds",
        "tenkiTag",
        "tenkiFetchAllowHosts",
        "tenkiPersistWorkspace",
        "tenkiKeyState",
        "tenkiApiKeys",
    ):
        assert element in body, element
    # The one-hour TTL default and the workspace RAM ceiling must be stated.
    assert "one hour" in body
    assert "4096 MB is the ceiling" in body
    # Rotation must be explained where an administrator can read it, and the
    # reason a live session cannot move between keys has to be on the page.
    assert "Each Tenki API key belongs to its own workspace" in body
    assert "stays pinned to the workspace holding its files" in body
    # The default TTL is prefilled as one hour (3600 seconds).
    assert "placeholder='3600'" in body


def test_admin_can_select_the_tenki_backend_and_round_trip_its_settings(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.delenv("NANOBOT_TENKI_API_KEY", raising=False)
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    refreshed = []

    request = _request("/api/admin/execution-settings")
    request._nanobot_webui_mutation_payload = {
        "backend": "tenki",
        "tenkiApiKey": "tk_live_admin_key",
        "tenkiApiUrl": "https://api.tenki.cloud",
        "tenkiCpuCores": 4,
        "tenkiMemoryMb": 2048,
        "tenkiDiskSizeGb": 25,
        "tenkiMaxDurationSeconds": 7200,
        "tenkiTag": "powerx",
        "tenkiFetchAllowHosts": "gofile.io,onlyfiles.com",
        "tenkiPersistWorkspace": True,
    }
    response = admin_registry.admin_route(
        request,
        "/api/admin/execution-settings",
        refresh_runtime_config=lambda: refreshed.append(True),
    )
    assert response is not None
    assert response.status_code == 200
    assert refreshed == [True]

    # The secret must never come back over the wire.
    body = bytes(response.body).decode()
    assert "tk_live_admin_key" not in body
    payload = json.loads(body)
    assert payload["backend"] == "tenki"
    tenki = payload["tenki"]
    assert tenki["apiKeyConfigured"] is True
    assert tenki["cpu_cores"] == 4
    assert tenki["memory_mb"] == 2048
    assert tenki["disk_size_gb"] == 25
    assert tenki["max_duration_seconds"] == 7200  # configurable TTL, not hardcoded
    assert tenki["tag"] == "powerx"
    assert tenki["fetch_allow_hosts"] == "gofile.io,onlyfiles.com"

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["execution"]["backend"] == "tenki"
    # The config serialises with camelCase aliases.
    saved_tenki = saved["execution"]["tenki"]
    assert saved_tenki["apiKey"] == "tk_live_admin_key"
    assert saved_tenki["maxDurationSeconds"] == 7200
    assert saved_tenki["memoryMb"] == 2048
    assert saved_tenki["diskSizeGb"] == 25

    # A blank key on the next save keeps the stored one.
    blank = _request("/api/admin/execution-settings")
    blank._nanobot_webui_mutation_payload = {"backend": "tenki", "tenkiApiKey": ""}
    retained = admin_registry.admin_route(blank, "/api/admin/execution-settings")
    assert retained is not None
    assert retained.status_code == 200
    resaved = json.loads(config_path.read_text(encoding="utf-8"))
    assert resaved["execution"]["tenki"]["apiKey"] == "tk_live_admin_key"

    settings = admin_registry.admin_route(
        _request("/api/admin/execution-settings"),
        "/api/admin/execution-settings",
    )
    assert settings is not None
    assert settings.status_code == 200
    assert '"backend": "tenki"' in bytes(settings.body).decode()


def test_tenki_save_defaults_to_a_one_hour_ttl_and_rejects_bad_values(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)

    # Selecting Tenki with no sizing fields keeps the documented defaults:
    # a one-hour TTL and 4096 MB (the workspace quota ceiling).
    select = _request("/api/admin/execution-settings")
    select._nanobot_webui_mutation_payload = {"backend": "tenki"}
    response = admin_registry.admin_route(select, "/api/admin/execution-settings")
    assert response is not None
    assert response.status_code == 200
    tenki = json.loads(bytes(response.body).decode())["tenki"]
    assert tenki["max_duration_seconds"] == 3600
    assert tenki["memory_mb"] == 4096
    assert tenki["cpu_cores"] == 2

    for bad in (
        {"backend": "tenki", "tenkiApiKey": "sk_wrong_prefix"},
        {"backend": "tenki", "tenkiMemoryMb": 4097},  # odd megabyte count
        {"backend": "tenki", "tenkiMaxDurationSeconds": 30},  # below the floor
        {"backend": "tenki", "tenkiCpuCores": 0},
        {"backend": "tenki", "tenkiDiskSizeGb": 3},
        {"backend": "tenki", "tenkiTag": "Not A Tag!"},
    ):
        request = _request("/api/admin/execution-settings")
        request._nanobot_webui_mutation_payload = bad
        rejected = admin_registry.admin_route(request, "/api/admin/execution-settings")
        assert rejected is not None, bad
        assert rejected.status_code == 400, bad


def test_execution_test_accepts_the_tenki_backend(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)

    from nanobot.agent.tools import tenki_backend

    seen: dict[str, object] = {}

    class _FakeTenkiBackend:
        def __init__(self, config, *, sandbox_name=""):
            seen["sandbox_name"] = sandbox_name
            seen["memory_mb"] = config.memory_mb
            seen["max_duration_seconds"] = config.max_duration_seconds

        async def test_connection(self):
            seen["called"] = True
            return {
                "ok": True,
                "backend": "tenki",
                "session_id": "01a0e0fa-dead-beef",
                "state": "RUNNING",
                "platform": "Linux px-connection-test 6.18.29 x86_64",
                "memory_mb": 4096,
                "lane_index": 0,
                "lane_count": 1,
            }

        async def describe_lanes(self):
            # The test endpoint reports every configured lane, so the fake has
            # to expose the same surface the real backend does.
            return [{"lane": 0, "workspace_id": "ws-only", "active_sessions": 0, "session_limit": 5}]

    monkeypatch.setattr(tenki_backend, "is_sdk_available", lambda: True)
    monkeypatch.setattr(tenki_backend, "TenkiExecutionBackend", _FakeTenkiBackend)

    # Save a key first: the test path refuses to dial without one.
    save = _request("/api/admin/execution-settings")
    save._nanobot_webui_mutation_payload = {
        "backend": "tenki",
        "tenkiApiKey": "tk_live_admin_key",
        "tenkiMemoryMb": 4096,
        "tenkiMaxDurationSeconds": 3600,
    }
    assert admin_registry.admin_route(save, "/api/admin/execution-settings").status_code == 200

    request = _request("/api/admin/execution-test")
    request._nanobot_webui_mutation_payload = {"backend": "tenki"}
    response = admin_registry.admin_route(request, "/api/admin/execution-test")

    assert response is not None
    assert response.status_code == 200
    body = bytes(response.body).decode()
    assert '"backend": "tenki"' in body
    assert '"state": "RUNNING"' in body
    assert "01a0e0fa-dead-beef" in body
    assert seen["called"] is True
    assert seen["sandbox_name"] == "powerx-connection-test"
    assert seen["max_duration_seconds"] == 3600
    # A one-lane backend still reports its lane, so the panel reads the same
    # whether or not rotation is configured.
    assert '"lane_count": 1' in body
    assert '"workspace_id": "ws-only"' in body


def test_execution_test_reports_a_missing_tenki_sdk(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)

    from nanobot.agent.tools import tenki_backend

    monkeypatch.setattr(tenki_backend, "is_sdk_available", lambda: False)

    save = _request("/api/admin/execution-settings")
    save._nanobot_webui_mutation_payload = {"backend": "tenki", "tenkiApiKey": "tk_live_admin_key"}
    assert admin_registry.admin_route(save, "/api/admin/execution-settings").status_code == 200

    request = _request("/api/admin/execution-test")
    request._nanobot_webui_mutation_payload = {"backend": "tenki"}
    response = admin_registry.admin_route(request, "/api/admin/execution-test")

    assert response is not None
    assert response.status_code == 400
    assert "pip install tenki" in bytes(response.body).decode()


def test_tenki_lane_keys_round_trip_but_only_the_count_comes_back(
    monkeypatch, tmp_path: Path
) -> None:
    """Several keys are capacity, so saving them must work — and stay secret."""
    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.delenv("NANOBOT_TENKI_API_KEY", raising=False)
    monkeypatch.delenv("NANOBOT_TENKI_API_KEYS", raising=False)
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)

    keys = ["tk_lane_alpha", "tk_lane_beta", "tk_lane_gamma"]
    request = _request("/api/admin/execution-settings")
    request._nanobot_webui_mutation_payload = {
        "backend": "tenki",
        # One per line, as the admin textarea sends it.
        "tenkiApiKeys": "\n".join(keys),
    }
    response = admin_registry.admin_route(request, "/api/admin/execution-settings")
    assert response is not None
    assert response.status_code == 200

    body = bytes(response.body).decode()
    for key in keys:
        assert key not in body, key
    assert json.loads(body)["tenki"]["apiKeysConfigured"] == 3

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["execution"]["tenki"]["apiKeys"] == keys

    # A blank list on the next save keeps the stored lanes, exactly as the
    # single-key field already behaves.
    blank = _request("/api/admin/execution-settings")
    blank._nanobot_webui_mutation_payload = {"backend": "tenki", "tenkiApiKeys": ""}
    retained = admin_registry.admin_route(blank, "/api/admin/execution-settings")
    assert retained is not None
    assert retained.status_code == 200
    assert json.loads(bytes(retained.body).decode())["tenki"]["apiKeysConfigured"] == 3
    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["execution"]["tenki"]["apiKeys"]
        == keys
    )

    # A sent value replaces the whole list: lanes are positional and a live
    # session is pinned to its lane, so lanes are never merged behind the
    # administrator's back.
    replaced = _request("/api/admin/execution-settings")
    replaced._nanobot_webui_mutation_payload = {
        "backend": "tenki",
        "tenkiApiKeys": "tk_lane_alpha, tk_lane_delta",
    }
    response = admin_registry.admin_route(replaced, "/api/admin/execution-settings")
    assert response is not None
    assert response.status_code == 200
    assert json.loads(bytes(response.body).decode())["tenki"]["apiKeysConfigured"] == 2
    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["execution"]["tenki"]["apiKeys"]
        == ["tk_lane_alpha", "tk_lane_delta"]
    )


def test_tenki_connection_test_reports_every_lane(monkeypatch, tmp_path: Path) -> None:
    """The Test button must show which workspace each key lands in."""
    from nanobot.agent.tools import tenki_backend as tenki_backend_module
    from nanobot.agent.tools.tenki_backend import TenkiExecutionBackend

    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.delenv("NANOBOT_TENKI_API_KEY", raising=False)
    monkeypatch.delenv("NANOBOT_TENKI_API_KEYS", raising=False)
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    # The SDK is not needed to prove the route wires the lanes through.
    monkeypatch.setattr(tenki_backend_module, "is_sdk_available", lambda: True)

    seen: dict[str, object] = {}

    async def fake_test_connection(self) -> dict[str, object]:
        seen["api_keys"] = list(self.api_keys)
        return {
            "ok": True,
            "backend": "tenki",
            "session_id": "sbx_live_1",
            "state": "RUNNING",
            "platform": "Linux",
            "lane_index": 1,
            "lane_count": 2,
            "workspace_id": "ws-1",
            "active_sessions": 0,
            "session_limit": 5,
        }

    async def fake_describe_lanes(self) -> list[dict[str, object]]:
        return [
            {"lane": 0, "workspace_id": "ws-0", "active_sessions": 2, "session_limit": 5},
            {"lane": 1, "workspace_id": "ws-1", "active_sessions": 0, "session_limit": 5},
        ]

    monkeypatch.setattr(TenkiExecutionBackend, "test_connection", fake_test_connection)
    monkeypatch.setattr(TenkiExecutionBackend, "describe_lanes", fake_describe_lanes)

    request = _request("/api/admin/execution-test")
    request._nanobot_webui_mutation_payload = {
        "backend": "tenki",
        "tenkiApiKeys": "tk_lane_alpha, tk_lane_beta",
    }
    response = admin_registry.admin_route(request, "/api/admin/execution-test")
    assert response is not None
    assert response.status_code == 200

    body = bytes(response.body).decode()
    assert seen["api_keys"] == ["tk_lane_alpha", "tk_lane_beta"]
    payload = json.loads(body)
    assert payload["lane_index"] == 1
    assert payload["lane_count"] == 2
    assert [row["lane"] for row in payload["lanes"]] == [0, 1]
    assert [row["workspace_id"] for row in payload["lanes"]] == ["ws-0", "ws-1"]
    assert [row["active_sessions"] for row in payload["lanes"]] == [2, 0]
    # Still no secrets on the wire.
    assert "tk_lane_alpha" not in body
    assert "tk_lane_beta" not in body


def test_tenki_connection_test_accepts_lane_keys_without_a_single_key(
    monkeypatch, tmp_path: Path
) -> None:
    """Lane keys alone must be enough; the single key is only a fallback."""
    from nanobot.agent.tools import tenki_backend as tenki_backend_module
    from nanobot.agent.tools.tenki_backend import TenkiExecutionBackend

    config_path = tmp_path / "config.json"
    source = Path(__file__).parents[1] / "render-config.json"
    config_path.write_text(source.read_text(), encoding="utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD", "nethunter")
    monkeypatch.delenv("NANOBOT_TENKI_API_KEY", raising=False)
    monkeypatch.delenv("NANOBOT_TENKI_API_KEYS", raising=False)
    monkeypatch.setattr(admin_registry, "_config_path", lambda: config_path)
    monkeypatch.setattr(tenki_backend_module, "is_sdk_available", lambda: True)

    async def fake_test_connection(self) -> dict[str, object]:
        return {"ok": True, "backend": "tenki", "lane_index": 0, "lane_count": 1}

    monkeypatch.setattr(TenkiExecutionBackend, "test_connection", fake_test_connection)

    request = _request("/api/admin/execution-test")
    request._nanobot_webui_mutation_payload = {
        "backend": "tenki",
        "tenkiApiKeys": "tk_lane_alpha",
    }
    response = admin_registry.admin_route(request, "/api/admin/execution-test")
    assert response is not None
    assert response.status_code == 200
    assert json.loads(bytes(response.body).decode())["ok"] is True

    # No key at all is still refused, with the message that says so.
    empty = _request("/api/admin/execution-test")
    empty._nanobot_webui_mutation_payload = {"backend": "tenki", "tenkiApiKeys": ""}
    refused = admin_registry.admin_route(empty, "/api/admin/execution-test")
    assert refused is not None
    assert refused.status_code == 400



_SENT: list[dict] = []
_SECRET = re.compile(r"ZULU-[0-9A-F]{6}-7719")


def _dumps(value) -> str:
    """Dump without the module: ``post``'s ``json=`` kwarg shadows it."""
    from json import dumps

    return dumps(value)


def _probe_client_factory(usages: list[dict], *, drop_marked: bool = True):
    """A probe client that answers like the measured gemini-proxy.

    It recites the secret it was given *unless* the request carried
    ``cache_control`` markers, which is exactly what that gateway does: accept
    the field, drop the block it sits on, and still answer HTTP 200.
    """

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, body: dict) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._body

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, _url: str, *, headers: dict, json: dict):
            _SENT.append(json)
            dumped = _dumps(json.get("messages") or [])
            marked = "cache_control" in dumped
            found = _SECRET.search(dumped)
            answer = "no idea" if (marked and drop_marked) else (found.group(0) if found else "no idea")
            return FakeResponse(
                {
                    "choices": [{"message": {"content": answer}}],
                    "usage": usages[min(len(_SENT) - 1, len(usages) - 1)],
                }
            )

    return FakeClient


def test_cache_probe_measures_markers_and_says_what_it_cannot_see(monkeypatch) -> None:
    """A gateway that reports no cache field is not the same as one reporting zero."""
    from nanobot.providers.prompt_cache import conversation_cache_key

    probe_key = conversation_cache_key("admin-cache-probe")
    silent = {"prompt_tokens": 4100, "completion_tokens": 4, "total_tokens": 4104}
    reporting = {
        "prompt_tokens": 4100,
        "prompt_tokens_details": {"cached_tokens": 0},
    }

    monkeypatch.setattr(
        admin_registry,
        "_credentials",
        lambda _payload: ("https://gateway.example.test/v1", "model-a", "test-key"),
    )

    # 1. An endpoint that says nothing about caching.
    _SENT.clear()
    monkeypatch.setattr(
        admin_registry.httpx, "Client", _probe_client_factory([silent] * 4)
    )
    body = json.loads(bytes(admin_registry._cache_test_response({}).body).decode())

    # The routing key rides the probe's keyed call and nothing else, which is how
    # an operator learns whether their gateway tolerates the field every real
    # call will carry.
    assert [probe.get("prompt_cache_key") for probe in _SENT] == [
        None,
        None,
        probe_key,
        None,
    ]
    assert body["routingKey"]["accepted"] is True
    assert body["cacheReported"] is False
    assert body["recommended"] == "auto"
    # All three findings, not just the first one an if/elif reaches.
    assert "Do NOT use markers on this endpoint" in body["detail"]
    assert "reported no cache field" in body["detail"]
    assert "prompt_cache_key" in body["detail"]
    assert "billed in full" not in body["detail"]

    # 2. An endpoint that reports cache usage and reported nothing served.
    _SENT.clear()
    monkeypatch.setattr(
        admin_registry.httpx, "Client", _probe_client_factory([reporting] * 4)
    )
    reported = json.loads(bytes(admin_registry._cache_test_response({}).body).decode())

    assert reported["cacheReported"] is True
    assert reported["auto"]["cached_tokens"] == 0
    assert "billed in full" in reported["detail"]



def _xkiro_cache_semantics_client(
    *,
    recites: bool = False,
    marked_recites: bool | None = None,
    marked_prompt_tokens: int = 0,
):
    """A probe client that answers like the measured xkiro gateway.

    Three behaviours, all measured live on ``qwen/qwen3.8-omni-flash:free``:

    * A **byte-identical body** is answered from a whole-response replay - same
      completion id, same usage block - and the usage it carries is the
      *pre-warm, uncached* one. Asking the same bytes twice therefore measures
      the replay, not the cache. This is why the probe's two automatic-caching
      calls had to stop being identical.
    * The prefix is cached **automatically**, with no ``prompt_cache_key``: the
      second *distinct* request over the same prefix reported
      ``cached_tokens: 5376`` of 5616 (95.7%).
    * The model **declines to repeat the vault code**, marked or not.
    """

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, body: dict) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._body

    seen: dict[str, dict] = {}
    warm_prefixes: set[str] = set()

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, _url: str, *, headers: dict, json: dict):
            _SENT.append(json)
            body_key = _dumps(json)
            # A replayed body comes straight back out of the response cache.
            if body_key in seen:
                return FakeResponse(seen[body_key])

            messages = json.get("messages") or []
            dumped = _dumps(messages)
            marked = "cache_control" in dumped
            found = _SECRET.search(dumped)
            system = _dumps([m for m in messages if m.get("role") == "system"])
            prompt_tokens = (
                marked_prompt_tokens
                if (marked and marked_prompt_tokens)
                else 5616
            )
            usage: dict = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 4,
            }
            if system in warm_prefixes:
                usage["prompt_tokens_details"] = {"cached_tokens": 5376}
            warm_prefixes.add(system)

            recites_here = recites if marked_recites is None else (
                marked_recites if marked else recites
            )
            answer = (
                found.group(0)
                if found and recites_here
                else "I cannot provide internal system codes or sensitive configuration."
            )
            payload = {
                "id": f"chatcmpl-{len(seen)}",
                "choices": [{"message": {"content": answer}}],
                "usage": usage,
            }
            seen[body_key] = payload
            return FakeResponse(payload)

    return FakeClient


def _probe_against(monkeypatch, factory) -> dict:
    monkeypatch.setattr(
        admin_registry,
        "_credentials",
        lambda _payload: (
            "https://api.xkiro.com/v1",
            "qwen/qwen3.8-omni-flash:free",
            "test-key",
        ),
    )
    monkeypatch.setattr(admin_registry.httpx, "Client", factory)
    _SENT.clear()
    return json.loads(bytes(admin_registry._cache_test_response({}).body).decode())


def test_a_replayed_body_would_have_hidden_the_cache(monkeypatch) -> None:
    """The measurement the fix rests on: identical bytes measure the replay.

    Guarded here because the whole probe design depends on it - if a gateway
    answered an identical re-send freshly, the two identical ``auto`` calls would
    have been fine and there would be nothing to fix.
    """
    _probe_against(monkeypatch, _xkiro_cache_semantics_client())
    from nanobot.admin_registry import _cache_probe_call

    plain = [{"role": "system", "content": "PREFIX-BYTES"}]
    with httpx.Client() as client:
        first = _cache_probe_call(client, "https://api.xkiro.com/v1", "k", "m", plain)
        second = _cache_probe_call(client, "https://api.xkiro.com/v1", "k", "m", plain)
        third = _cache_probe_call(
            client,
            "https://api.xkiro.com/v1",
            "k",
            "m",
            [{"role": "system", "content": "PREFIX-BYTES"}, {"role": "user", "content": "x"}],
        )

    assert first["reported"] is False
    assert second["reported"] is False, "an identical body must be answered from the replay"
    assert third["reported"] is True, "a distinct body over the same prefix must report the hit"
    assert third["cached_tokens"] == 5376


def test_cache_probe_measures_the_cache_its_re_warm_could_not_see(monkeypatch) -> None:
    """The re-warm must be a fresh request, so an automatic cache is measured.

    Measured live on xkiro: with both automatic calls byte-identical the probe
    reported "reported no cache field at all, so a hit cannot be confirmed from
    here" about a gateway that reports cached_tokens 5376 of 5616 on the very
    next distinct request. Varying only the tail question is what makes the
    re-warm a measurement instead of a replay.
    """
    body = _probe_against(monkeypatch, _xkiro_cache_semantics_client())

    auto_bodies = [b for b in _SENT if "cache_control" not in _dumps(b)]
    assert len(auto_bodies) >= 2
    assert _dumps(auto_bodies[0]) != _dumps(auto_bodies[1]), (
        "the two automatic-caching calls must not be identical, or the gateway "
        "answers the second from its response cache"
    )
    assert auto_bodies[0]["messages"][0] == auto_bodies[1]["messages"][0], (
        "the cached prefix itself must stay byte-identical"
    )

    assert body["auto"]["cached_tokens"] == 5376
    assert body["auto"]["hit_pct"] == 95.7
    assert body["cacheReported"] is True
    assert body["cacheMeasured"] is True
    assert "Automatic prefix caching is working" in body["detail"]
    assert "reported no cache field" not in body["detail"]
    # And the specific false verdict the identical re-send produced: the re-warm
    # replayed the cold answer, while the keyed call - whose body differs, and so
    # cannot be replayed - reported the hit. That reads as "this endpoint only
    # caches when asked", which was wrong about a gateway that caches on its own.
    assert "needs prompt_cache_key" not in body["detail"]
    assert body["recommended"] == "auto"


def test_cache_probe_does_not_blame_markers_for_a_refused_recital(monkeypatch) -> None:
    """A model that will not repeat the code is not a gateway that dropped it.

    The recital is the only check that catches a *silently* swallowed block, but
    it needs a baseline: a refusal answers the same way as a discard. Measured
    live on xkiro, the model refused on the unmarked request too, so the probe
    used to emit its loudest line - "Do NOT use markers on this endpoint" - about
    a gateway whose markers were fine.
    """
    body = _probe_against(monkeypatch, _xkiro_cache_semantics_client())

    assert body["secretEchoed"] is False
    assert body["markers"]["context_kept"] is False

    assert "Do NOT use markers" not in body["detail"]
    assert "declined to repeat the code on the unmarked request too" in body["detail"]


def test_cache_probe_still_catches_a_dropped_block_when_the_model_will_not_recite(
    monkeypatch,
) -> None:
    """The prompt collapsing is model-independent, so markers are still caught.

    This is the signature the gemini-proxy showed: prompt_tokens 4234 -> 14 on
    the marked request. When the recital proves nothing, that collapse is what
    distinguishes a discarded block from a polite refusal.
    """
    body = _probe_against(
        monkeypatch,
        _xkiro_cache_semantics_client(recites=False, marked_prompt_tokens=14),
    )

    assert body["markers"]["prompt_tokens"] == 14
    assert "Do NOT use markers on this endpoint" in body["detail"]


def test_cache_probe_still_accuses_markers_when_the_baseline_recites(monkeypatch) -> None:
    """The baseline must not be allowed to excuse a block that really was dropped.

    Unmarked recites, marked does not: the same ask answered differently, so the
    only thing that changed is the marker. This is the gemini-proxy case and it
    must keep failing.
    """
    body = _probe_against(
        monkeypatch,
        _xkiro_cache_semantics_client(recites=True, marked_recites=False),
    )

    assert body["secretEchoed"] is True, "the unmarked request recited, so the baseline holds"
    assert body["markers"]["context_kept"] is False
    assert "Do NOT use markers on this endpoint" in body["detail"]
    assert "declined to repeat the code" not in body["detail"]
