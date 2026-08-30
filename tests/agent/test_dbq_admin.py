from __future__ import annotations

import json

import pytest

from nanobot import dbq_admin


def test_gateway_uses_render_aliases_without_returning_key(monkeypatch) -> None:
    monkeypatch.delenv("DBQ_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DBQ_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("UNIABUJA_DBQ_URL", "https://transcript.example.test/upload_files/dbq.php")
    monkeypatch.setenv("UNIABUJA_DBQ_KEY", "render-secret")
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return [{"TABLE_NAME": "studenttb"}]

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(dbq_admin.httpx, "post", fake_post)
    result = dbq_admin.sql("SHOW TABLES")
    assert result == [{"TABLE_NAME": "studenttb"}]
    assert captured["url"].startswith("https://")
    assert captured["headers"]["X-DBQ-Key"] == "render-secret"


def test_identifiers_and_generic_writes_are_strict(monkeypatch) -> None:
    with pytest.raises(dbq_admin.DBQValidationError):
        dbq_admin.read_table("studenttb; DROP TABLE users")

    statements = []
    monkeypatch.setattr(dbq_admin, "_schema_rows", lambda _table: [
        {"COLUMN_NAME": "id", "COLUMN_KEY": "PRI"},
        {"COLUMN_NAME": "status", "COLUMN_KEY": ""},
    ])
    def fake_sql(query):
        statements.append(query)
        if query.startswith("SELECT"):
            return [{"id": 7, "status": "Active"}]
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.generic_update("studenttb", {"id": 7}, {"status": "Active", "id": 99})
    assert result["affected_rows"] == 1
    update_statement = next(statement for statement in statements if statement.startswith("UPDATE"))
    assert "`id`=99" not in update_statement
    assert "`status`='Active'" in update_statement
    with pytest.raises(dbq_admin.DBQValidationError):
        dbq_admin.generic_delete("studenttb", {"id": 7}, False)


def test_score_update_encodes_scores_and_verifies(monkeypatch) -> None:
    calls = []
    before = [{"regno": "22/205EEE/132", "course_code": "FEG412", "final_ca": "old"}]
    after = [{"regno": "22/205EEE/132", "course_code": "FEG412", "final_ca": "encoded"}]

    def fake_sql(query):
        calls.append(query)
        if query.startswith("UPDATE"):
            return {"affected_rows": 1}
        return before if len([c for c in calls if c.startswith("SELECT")]) == 1 else after

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.score_update({
        "regno": "22/205EEE/132",
        "course": "FEG412",
        "session": "2025/2026",
        "semester": "1st",
        "ca": "20",
        "exam": "20",
        "operator": "ACA2538",
        "modifier": "ACA_ADMIN",
        "publish": True,
    })
    assert result["after"] == after[0]
    update = next(call for call in calls if call.startswith("UPDATE"))
    assert "score_encode(20)" in update
    assert "score_encode(20)" in update
    assert "upload_status='Yes'" in update


def test_catalog_shape_supports_all_live_tables(monkeypatch) -> None:
    monkeypatch.setenv("DBQ_GATEWAY_URL", "https://transcript.example.test/dbq")
    monkeypatch.setenv("DBQ_GATEWAY_KEY", "secret")
    monkeypatch.setattr(dbq_admin, "sql", lambda _query: [{"TABLE_NAME": f"table_{i}"} for i in range(208)])
    result = dbq_admin.catalog()
    assert result["table_count"] == 208
    assert result["tables"][0]["TABLE_NAME"] == "table_0"


def test_dbq_rows_redact_password_like_values() -> None:
    rows = dbq_admin._rows([
        {"regno": "S1", "password": "student-secret", "password_hash": "hash-value", "status": "Active"},
    ], "test rows")

    assert rows == [{
        "regno": "S1",
        "password": "[REDACTED]",
        "password_hash": "[REDACTED]",
        "status": "Active",
    }]


def test_generic_update_rejects_password_fields(monkeypatch) -> None:
    monkeypatch.setattr(dbq_admin, "_schema_rows", lambda _table: [
        {"COLUMN_NAME": "regno", "COLUMN_KEY": "PRI"},
        {"COLUMN_NAME": "password", "COLUMN_KEY": ""},
    ])

    with pytest.raises(dbq_admin.DBQValidationError, match="dedicated admin operation"):
        dbq_admin.generic_update("studenttb", {"regno": "S1"}, {"password": "new-secret"})


def test_student_password_reset_matches_existing_hash_format_and_never_returns_password(monkeypatch) -> None:
    import hashlib

    statements: list[str] = []
    stored_hash = hashlib.md5(b"old-student-secret", usedforsecurity=False).hexdigest()
    expected_hash = hashlib.md5(b"new-student-secret", usedforsecurity=False).hexdigest()
    select_count = 0

    def fake_sql(query):
        nonlocal select_count
        statements.append(query)
        if query.startswith("SELECT regno,password FROM studenttb"):
            select_count += 1
            if select_count == 1:
                return [{"regno": "S1", "password": stored_hash}]
            return [{"regno": "S1", "password": expected_hash}]
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    payload = {
        "regno": "S1",
        "newPassword": "new-student-secret",
        "confirmPassword": "new-student-secret",
        "operator": "ACA_ADMIN",
        "reason": "Verified support ticket SUP-123",
        "confirmed": True,
    }

    result = dbq_admin.student_password_reset(payload)

    assert result["ok"] is True
    assert result["regno"] == "S1"
    assert "new-student-secret" not in json.dumps(result)
    assert any(
        query.startswith("UPDATE studenttb SET password=")
        and "WHERE regno='S1'" in query
        and expected_hash in query
        and "new-student-secret" not in query
        for query in statements
    )

    assert dbq_admin._password_storage_format(stored_hash) == "md5"
    assert dbq_admin._password_storage_format("plain-value") == "plain"
    assert dbq_admin._password_storage_format("$2b$12$example") == "bcrypt"
    portal_hash = dbq_admin._encode_student_password("sample-secret", "sha512_md5_uapro")
    assert len(portal_hash) == 160
    assert dbq_admin._password_storage_format(portal_hash) == "sha512_md5_uapro"
    assert dbq_admin._student_password_matches("sample-secret", portal_hash, "sha512_md5_uapro")

    with pytest.raises(dbq_admin.DBQValidationError, match="explicit confirmation"):
        dbq_admin.student_password_reset(dict(payload, confirmed=False))
    with pytest.raises(dbq_admin.DBQValidationError, match="do not match"):
        dbq_admin.student_password_reset(dict(payload, confirmPassword="different"))


def test_student_password_format_diagnostics_returns_only_safe_metadata(monkeypatch) -> None:
    calls: list[str] = []

    def fake_sql(query):
        calls.append(query)
        if query.startswith("SELECT regno,CHAR_LENGTH"):
            return [{"regno": "S1", "password_length": 32, "password_format": "md5"}]
        if query.startswith("SELECT username,CHAR_LENGTH"):
            return [{"username": "S1", "password_length": 32, "status": "Active", "password_format": "md5"}]
        return [{"total_count": 1, "empty_count": 0, "md5_count": 1, "sha1_count": 0, "sha256_count": 0, "bcrypt_count": 0}]

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.student_password_format_diagnostics({"prefix": "S"})

    assert result["ok"] is True
    assert result["studenttb"] == [{"regno": "S1", "password_length": 32, "password_format": "md5"}]
    assert result["trans_users"] == [{"username": "S1", "password_length": 32, "status": "Active", "password_format": "md5"}]
    assert result["observed_formats"] == ["md5"]
    assert "password_value" not in json.dumps(result)
    assert all("SELECT password" not in query for query in calls)


def test_student_password_reset_infers_majority_format_after_plaintext_overwrite(monkeypatch) -> None:
    import hashlib

    statements: list[str] = []
    expected_hash = hashlib.md5(b"replacement-secret", usedforsecurity=False).hexdigest()
    select_count = 0

    def fake_sql(query):
        nonlocal select_count
        statements.append(query)
        if query.startswith("SELECT regno,password FROM studenttb"):
            select_count += 1
            return [{"regno": "S1", "password": "william2023"}] if select_count == 1 else [{"regno": "S1", "password": expected_hash}]
        if query.startswith("SELECT COUNT(*) AS total_count"):
            return [{"total_count": 6, "md5_count": 5, "sha1_count": 0, "sha256_count": 0, "sha512_md5_uapro_count": 0, "bcrypt_count": 0}]
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.student_password_reset({
        "regno": "S1",
        "newPassword": "replacement-secret",
        "confirmPassword": "replacement-secret",
        "operator": "ACA_ADMIN",
        "reason": "Verified support ticket SUP-124",
        "confirmed": True,
    })

    assert result["ok"] is True
    assert result["password_format_source"] == "student-table majority"
    assert expected_hash in next(query for query in statements if query.startswith("UPDATE studenttb"))


def test_student_password_reset_infers_portal_160_format(monkeypatch) -> None:
    statements: list[str] = []
    expected = dbq_admin._encode_student_password("replacement-secret", "sha512_md5_uapro")
    select_count = 0

    def fake_sql(query):
        nonlocal select_count
        statements.append(query)
        if query.startswith("SELECT regno,password FROM studenttb"):
            select_count += 1
            return [{"regno": "S1", "password": "william2023"}] if select_count == 1 else [{"regno": "S1", "password": expected}]
        if query.startswith("SELECT COUNT(*) AS total_count"):
            return [{"total_count": 10, "md5_count": 0, "sha1_count": 0, "sha256_count": 0, "sha512_md5_uapro_count": 9, "bcrypt_count": 0}]
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.student_password_reset({
        "regno": "S1",
        "newPassword": "replacement-secret",
        "confirmPassword": "replacement-secret",
        "operator": "ACA_ADMIN",
        "reason": "Verified support ticket SUP-125",
        "confirmed": True,
    })
    assert result["ok"] is True
    assert result["password_format_source"] == "student-table majority"
    update = next(query for query in statements if query.startswith("UPDATE studenttb"))
    assert expected in update
    assert "replacement-secret" not in update


def test_generic_payload_never_returns_configured_gateway_secret(monkeypatch) -> None:
    monkeypatch.setenv("DBQ_GATEWAY_KEY", "do-not-return")
    monkeypatch.setattr(dbq_admin, "_schema_rows", lambda _table: [{"COLUMN_NAME": "id", "COLUMN_KEY": "PRI"}])
    monkeypatch.setattr(dbq_admin, "sql", lambda _query: {"affected_rows": 1})
    payload = dbq_admin.generic_delete("studenttb", {"id": 1}, True)
    assert "do-not-return" not in json.dumps(payload)


def test_payment_check_and_eligibility_require_matching_verified_records(monkeypatch) -> None:
    monkeypatch.setattr(dbq_admin, "_payment_rows", lambda _payload: [{
        "id": 7, "regno": "22/205EEE/132", "amount": "150000.00", "payment_status": "Full",
        "response_code": "01", "fee_code": "5386252962", "pay_item_id": "5386252962",
        "trans_id": "TRANS-1", "rrr": "RRR-1", "session": "2025/2026",
    }])
    monkeypatch.setattr(dbq_admin, "_payment_detail_summary", lambda _row: {
        "detail_lines": 2, "detail_total": "150000.00", "pay_item_id": "5386252962",
    })
    result = dbq_admin.payment_check({"regno": "22/205EEE/132", "session": "2025/2026", "semester": "1st"})
    assert result["eligible"] is True
    assert dbq_admin.payment_eligibility({"regno": "22/205EEE/132", "session": "2025/2026", "semester": "1st"})["registration_payment_gate"] is True


def test_payment_add_validates_detail_total_and_writes_audit(monkeypatch) -> None:
    statements = []

    def fake_sql(query):
        statements.append(query)
        if "FROM studenttb" in query:
            return [{"regno": "22/205EEE/132", "prog_id": "ELC", "portal_category_code": "ug"}]
        if "duplicate_count" in query:
            return [{"duplicate_count": 0}]
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    monkeypatch.setattr(dbq_admin, "payment_check", lambda _payload: {"operation": "payment_check", "eligible": True})
    payload = {
        "regno": "22/205EEE/132", "session": "2025/2026", "semester": "1st", "amount": "150000",
        "receipt": "REC-1", "rrr": "RRR-1", "transId": "TRANS-1", "paymentDate": "2026-01-10",
        "paymentTime": "10:00:00", "payItemId": "5386252962", "feeCode": "5386252962",
        "prog": "ELC", "level": "400", "operator": "BURSAR_ADMIN", "verificationRef": "BANK-1",
        "details": [{"folio_code": "TUITION", "amount": "100000"}, {"folio_code": "LEVY", "amount": "50000"}],
    }
    result = dbq_admin.payment_add(payload)
    assert result["ok"] is True
    assert any(query.startswith("INSERT INTO portal_paymenttb") for query in statements)
    assert any("portal_payment_detailtb" in query for query in statements)
    assert any("audit_logs_payment" in query for query in statements)
    with pytest.raises(dbq_admin.DBQValidationError):
        bad = dict(payload, details=[{"folio_code": "TUITION", "amount": "1"}])
        dbq_admin.payment_add(bad)


def test_payment_reconcile_refuses_mismatched_amounts(monkeypatch) -> None:
    monkeypatch.setattr(dbq_admin, "_payment_rows", lambda _payload: [{
        "id": 7, "regno": "22/205EEE/132", "amount": "150000.00", "payment_status": "Full",
        "response_code": "01", "pay_item_id": "5386252962", "fee_code": "OLD",
        "trans_id": "TRANS-1", "rrr": "RRR-1", "session": "2025/2026",
    }])
    monkeypatch.setattr(dbq_admin, "_payment_detail_summary", lambda _row: {"detail_total": "149999.00", "pay_item_id": "5386252962"})
    with pytest.raises(dbq_admin.DBQValidationError, match="amounts differ"):
        dbq_admin.payment_reconcile({"regno": "22/205EEE/132", "session": "2025/2026", "semester": "1st", "modifier": "ADMIN", "verificationRef": "BANK-1"})


def test_student_payments_returns_main_and_detail_rows(monkeypatch) -> None:
    calls = []

    def fake_sql(query):
        calls.append(query)
        if "FROM studenttb" in query:
            return [{"regno": "S1", "surname": "Doe"}]
        if "FROM portal_paymenttb" in query:
            return [{"id": 11, "regno": "S1", "trans_id": "T1", "rrr": "R1", "session": "2025/2026", "semester": "1st"}]
        return [{"id": 12, "regno": "S1", "folio_code": "TUITION", "trans_id": "T1", "rrr": "R1"}]

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.student_payments({"regno": "S1", "session": "2025/2026", "semester": "1st"})
    assert result["student"]["regno"] == "S1"
    assert result["payments"][0]["id"] == 11
    assert result["details"][0]["id"] == 12
    assert any("portal_payment_detailtb" in query for query in calls)


def test_payment_update_main_is_whitelisted_audited_and_verified(monkeypatch) -> None:
    statements = []
    rows = [{"id": 11, "amount": "100.00", "fee_code": "OLD", "trans_id": "T1", "rrr": "R1"}]

    def fake_sql(query):
        statements.append(query)
        if "SELECT * FROM `portal_paymenttb`" in query:
            return rows
        if "audit_logs_payment" in query:
            return {"affected_rows": 1}
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.payment_update({
        "recordType": "main", "recordId": 11, "operator": "ADMIN", "verificationRef": "BANK-REF",
        "changes": {"fee_code": "NEW", "response_desc": "Verified"},
    })
    assert result["ok"] is True
    assert any("`fee_code`='NEW'" in query for query in statements)
    assert any("audit_logs_payment" in query for query in statements)
    with pytest.raises(dbq_admin.DBQValidationError, match="cannot be edited"):
        dbq_admin.payment_update({
            "recordType": "main", "recordId": 11, "operator": "ADMIN", "verificationRef": "BANK-REF",
            "changes": {"id": 99},
        })


def test_payment_update_amount_requires_confirmation_and_matching_detail_total(monkeypatch) -> None:
    monkeypatch.setattr(dbq_admin, "sql", lambda _query: [{"id": 11, "amount": "100.00", "trans_id": "T1", "rrr": "R1"}])
    monkeypatch.setattr(dbq_admin, "_payment_detail_summary", lambda _row: {"detail_total": "100.00"})
    base = {"recordType": "main", "recordId": 11, "operator": "ADMIN", "verificationRef": "BANK-REF", "changes": {"amount": "100.00"}}
    with pytest.raises(dbq_admin.DBQValidationError, match="confirmAmount"):
        dbq_admin.payment_update(base)
    with pytest.raises(dbq_admin.DBQValidationError, match="equal.*detail total"):
        dbq_admin.payment_update({**base, "confirmAmount": True, "changes": {"amount": "101.00"}})


def test_catalog_includes_map_roles_and_write_guidance(monkeypatch) -> None:
    def fake_sql(query):
        if "information_schema.TABLES" in query:
            return [{"TABLE_NAME": "portal_paymenttb", "TABLE_TYPE": "BASE TABLE"}, {"TABLE_NAME": "studenttb"}]
        if "information_schema.COLUMNS" in query:
            return [
                {"TABLE_NAME": "portal_paymenttb", "COLUMN_NAME": "id", "COLUMN_KEY": "PRI"},
                {"TABLE_NAME": "portal_paymenttb", "COLUMN_NAME": "amount", "COLUMN_KEY": ""},
                {"TABLE_NAME": "studenttb", "COLUMN_NAME": "regno", "COLUMN_KEY": "PRI"},
            ]
        return [{"TABLE_NAME": "portal_paymenttb", "CONSTRAINT_NAME": "PRIMARY", "COLUMN_NAME": "id"}]

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.catalog()
    payment = next(item for item in result["tables"] if item["TABLE_NAME"] == "portal_paymenttb")
    assert result["table_count"] == 2
    assert payment["purpose"] == "Main payment ledger"
    assert payment["primary_columns"] == ["id"]
    assert payment["can_update_delete"] is True
    assert any(column["role"] == "Monetary amount." for column in payment["columns"])


def test_score_add_rejects_duplicates_and_verifies_insert(monkeypatch) -> None:
    calls = []

    def fake_sql(query):
        calls.append(query)
        if query.startswith("SELECT") and "regtb" in query:
            return [] if len([item for item in calls if item.startswith("SELECT")]) == 1 else [{"regno": "S1", "course_code": "C1"}]
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.score_add({
        "regno": "S1", "course": "C1", "session": "2025/2026", "semester": "1st", "title": "Course One",
        "dept": "EEE", "prog": "ELC", "level": "400", "unit": 3, "ca": 20, "exam": 60,
        "operator": "ADMIN", "modifier": "ADMIN", "portalCategory": "ug",
    })
    assert result["ok"] is True
    assert any(query.startswith("INSERT INTO regtb") and "score_encode(20)" in query for query in calls)
    assert any(query.startswith("SELECT") and "regtb" in query for query in calls[1:])


def test_batch_and_map_diagnostics_validate_and_return_sections(monkeypatch) -> None:
    monkeypatch.setattr(dbq_admin, "sql", lambda _query: [])
    batch = dbq_admin.batch_check({"course": "C1", "session": "2025/2026", "semester": "1st"})
    mapped = dbq_admin.map_check({"course": "C1", "session": "2025/2026", "semester": "1st", "prog": "ELC", "portalCategory": "ug"})
    assert set(batch) >= {"row_counts", "unpublished", "upload_files", "allocations"}
    assert set(mapped) >= {"master_course", "session_curriculum", "allocations"}


def test_payment_detail_update_requires_evidence_and_updates_allowed_fields(monkeypatch) -> None:
    statements = []

    def fake_sql(query):
        statements.append(query)
        if "SELECT * FROM `portal_payment_detailtb`" in query:
            return [{"id": 12, "folio_code": "OLD", "amount": "100.00", "trans_id": "T1", "rrr": "R1"}]
        if "audit_logs_payment" in query:
            return {"affected_rows": 1}
        return {"affected_rows": 1}

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.payment_update({
        "recordType": "detail", "recordId": 12, "operator": "ADMIN", "verificationRef": "BANK-REF",
        "changes": {"folio_code": "UPDATED"},
    })
    assert result["ok"] is True
    assert any("`folio_code`='UPDATED'" in query for query in statements)
    with pytest.raises(dbq_admin.DBQValidationError, match="cannot be edited"):
        dbq_admin.payment_update({
            "recordType": "detail", "recordId": 12, "operator": "ADMIN", "verificationRef": "BANK-REF",
            "changes": {"user_password": "x"},
        })


def test_portal_workspaces_group_staff_students_courses_and_results(monkeypatch) -> None:
    monkeypatch.setattr(dbq_admin, "catalog", lambda _search="": {
        "database": "portal",
        "table_count": 6,
        "tables": [
            {"TABLE_NAME": "studenttb", "purpose": "Student master records", "handles": "Student identity", "primary_columns": ["regno"], "can_update_delete": True},
            {"TABLE_NAME": "stafftb", "purpose": "Staff master records", "handles": "Staff identity", "primary_columns": ["id"], "can_update_delete": True},
            {"TABLE_NAME": "staff_roletb", "purpose": "Staff role assignments", "handles": "HOD/dean roles", "primary_columns": ["id"], "can_update_delete": True},
            {"TABLE_NAME": "coursetb", "purpose": "Master course catalogue", "handles": "Course title", "primary_columns": ["id"], "can_update_delete": True},
            {"TABLE_NAME": "regtb", "purpose": "Live course registrations and results", "handles": "Grades and publication", "primary_columns": ["id"], "can_update_delete": True},
            {"TABLE_NAME": "audit_logs_payment", "purpose": "Payment audit log", "handles": "Payment audit", "primary_columns": ["id"], "can_update_delete": True},
        ],
    })
    result = dbq_admin.portal_workspaces()
    grouped = {item["title"]: {table["name"] for table in item["tables"]} for item in result["workspaces"]}
    assert "studenttb" in grouped["Students"]
    assert {"stafftb", "staff_roletb"} <= grouped["Staff, HOD, dean and roles"]
    assert "coursetb" in grouped["Courses and curriculum"]
    assert "regtb" in grouped["Students"] or "regtb" in grouped["Grades and results"]
    assert "audit_logs_payment" in grouped["Payments and finance"]


def test_portal_workspaces_preserves_search_and_write_metadata(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(dbq_admin, "catalog", lambda search="": (calls.append(search) or {
        "database": "portal", "table_count": 1,
        "tables": [{"TABLE_NAME": "staff_roletb", "purpose": "Staff role assignments", "handles": "HOD/dean roles", "primary_columns": ["id"], "can_update_delete": False, "write_guidance": "Read-only guidance"}],
    }))
    result = dbq_admin.portal_workspaces("hod")
    assert calls == ["hod"]
    item = next(table for group in result["workspaces"] for table in group["tables"] if table["name"] == "staff_roletb")
    assert item["primary_columns"] == ["id"]
    assert item["can_update_delete"] is False
    assert item["write_guidance"] == "Read-only guidance"


def test_admin_page_contains_dynamic_portal_workspace_controls() -> None:
    from nanobot import admin_registry

    html = admin_registry._dbq_admin_section()
    assert "dbqLoadWorkspaces" in html
    assert "/api/admin/dbq/workspaces" in html
    assert "student/staff/course workspaces" in html
    assert "dbqLoadStudentPayments" in html


def test_admin_payment_workspace_contains_no_code_manual_editor() -> None:
    from nanobot import admin_registry

    html = admin_registry._dbq_admin_section()
    for marker in (
        "dbqPaymentRecordCards", "dbqPaymentManualEditor", "Edit fields",
        "dbqPaymentManualUpdate", "dbqPaymentVerificationRef",
        "No JSON or coding is required", "Advanced JSON editor",
        "manualPaymentFields", "renderPaymentRecordCards", "payment_update",
    ):
        assert marker in html


def test_read_table_honors_bounded_pagination(monkeypatch) -> None:
    calls = []

    def fake_sql(query):
        calls.append(query)
        if "information_schema.COLUMNS" in query:
            return [{"COLUMN_NAME": "id", "COLUMN_KEY": "PRI"}]
        return [{"id": 7}]

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.read_table("studenttb", 200, 400)
    assert result["limit"] == 200
    assert result["offset"] == 400
    assert any("LIMIT 200 OFFSET 400" in query for query in calls)
    with pytest.raises(dbq_admin.DBQValidationError):
        dbq_admin.read_table("studenttb", "bad", 0)


def test_admin_database_ui_exposes_complete_table_editor_controls() -> None:
    from nanobot import admin_registry

    html = admin_registry._admin_page([])
    for marker in (
        "dbqLoadCatalog", "dbqLoadWorkspaces", "dbqLoadSchema", "dbqLoadRows",
        "select.add(new Option(name,name));select.value=name",
        "dbqPrevRows", "dbqNextRows", "dbqSchemaView", "dbqRowsView", "Edit row",
        "dbqInsert", "dbqUpdate", "dbqDelete", "/api/admin/dbq/workspaces",
        "@media (max-width:680px)",
    ):
        assert marker in html


def test_admin_workspace_route_is_allowlisted_and_requires_auth(monkeypatch) -> None:
    from nanobot import admin_registry

    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")

    class Request:
        headers = {}
        path = "/api/admin/dbq/workspaces"
        method = "GET"

    response = admin_registry.admin_route(Request(), Request.path)
    assert response.status_code == 401


def test_table_search_uses_schema_text_and_reference_columns(monkeypatch) -> None:
    calls = []

    def fake_sql(query):
        calls.append(query)
        if "information_schema.COLUMNS" in query:
            return [
                {"COLUMN_NAME": "id", "COLUMN_TYPE": "int", "COLUMN_KEY": "PRI"},
                {"COLUMN_NAME": "course_code", "COLUMN_TYPE": "varchar(32)", "COLUMN_KEY": ""},
                {"COLUMN_NAME": "status", "COLUMN_TYPE": "enum('A','I')", "COLUMN_KEY": ""},
            ]
        return [{"id": 3, "course_code": "FEG412", "status": "A"}]

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.table_search("coursetb", "FEG412", 999)
    assert result["limit"] == 100
    assert result["rows"][0]["course_code"] == "FEG412"
    search_query = next(query for query in calls if "LIKE LOWER" in query)
    assert "`course_code`" in search_query
    assert "`status`" in search_query
    assert "FEG412" in search_query
    with pytest.raises(dbq_admin.DBQValidationError):
        dbq_admin.table_search("course;drop", "FEG412")
    with pytest.raises(dbq_admin.DBQValidationError):
        dbq_admin.table_search("coursetb", "x")


def test_person_search_returns_student_and_staff_matches(monkeypatch) -> None:
    calls = []

    def fake_schema(table):
        return [{"COLUMN_NAME": "name", "COLUMN_TYPE": "varchar(120)"}, {"COLUMN_NAME": "id", "COLUMN_TYPE": "int"}]

    def fake_sql(query):
        calls.append(query)
        if "stafftb" in query:
            return [{"id": 2, "name": "Ada Staff"}]
        if "staff_emailtb" in query:
            return [{"id": 3, "name": "Ada Staff", "email": "ada@example.test"}]
        return [{"regno": "S1", "name": "Ada Student"}]

    monkeypatch.setattr(dbq_admin, "_schema_rows", fake_schema)
    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.person_search("Ada", "all", 20)
    assert result["scope"] == "all"
    assert {item["table"] for item in result["matches"]} == {"studenttb", "stafftb", "staff_emailtb"}
    assert len(calls) == 3
    with pytest.raises(dbq_admin.DBQValidationError):
        dbq_admin.person_search("Ada", "deans")


def test_admin_database_ui_exposes_person_and_table_search_controls() -> None:
    from nanobot import admin_registry

    html = admin_registry._admin_page([])
    for marker in (
        "dbqPersonTerm", "dbqPersonScope", "dbqPersonSearch", "dbqPersonResults",
        "dbqTableTerm", "dbqTableSearch", "dbqTableSearchResults",
        "/api/admin/dbq/person-search", "/api/admin/dbq/table-search",
    ):
        assert marker in html


def test_search_routes_are_allowlisted_and_unauthenticated_requests_are_rejected(monkeypatch) -> None:
    from nanobot import admin_registry

    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")

    class Request:
        headers = {}
        method = "GET"

    for path in ("/api/admin/dbq/person-search", "/api/admin/dbq/table-search"):
        request = Request()
        request.path = path
        response = admin_registry.admin_route(request, path)
        assert response.status_code == 401


def test_student_lookup_uses_indexable_regno_and_returns_raw_scores(monkeypatch) -> None:
    calls = []

    def fake_sql(query, *, timeout=60.0):
        calls.append((query, timeout))
        if "FROM studenttb" in query:
            return [{"regno": "S1", "surname": "Student", "first_name": "One"}]
        return [{"regno": "S1", "course_code": "C1", "final_ca": "enc-ca", "final_exam": "enc-exam"}]

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.student_lookup("S1", "2025/2026")
    assert result["student"]["regno"] == "S1"
    assert result["result_status"] == "loaded"
    assert result["results"][0]["course_code"] == "C1"
    assert all(timeout == 20.0 for _, timeout in calls)
    assert all("LOWER(regno)" not in query for query, _ in calls)
    assert any("session='2025/2026'" in query for query, _ in calls)


def test_student_lookup_preserves_student_when_result_gateway_times_out(monkeypatch) -> None:
    def fake_sql(query, *, timeout=60.0):
        if "FROM studenttb" in query:
            return [{"regno": "S1", "surname": "Student"}]
        raise dbq_admin.DBQError("Database gateway request failed: ConnectTimeout")

    monkeypatch.setattr(dbq_admin, "sql", fake_sql)
    result = dbq_admin.student_lookup("S1", "2025/2026")
    assert result["student"]["regno"] == "S1"
    assert result["results"] == []
    assert result["result_status"] == "unavailable"
    assert result["result_error"] == "Database gateway request failed: ConnectTimeout"


def test_admin_database_ui_exposes_partial_student_result_status_hook() -> None:
    from nanobot import admin_registry

    html = admin_registry._admin_page([])
    assert "v.result_status==='unavailable'" in html
    assert "Student loaded, but results are unavailable" in html
    assert "first.final_ca" in html
