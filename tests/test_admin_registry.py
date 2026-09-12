import base64
import json
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
