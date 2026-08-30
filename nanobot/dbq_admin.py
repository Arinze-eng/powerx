"""Admin-only database gateway operations for the DBQ portal.

The gateway URL and credential are read exclusively from environment variables:
DBQ_GATEWAY_URL, DBQ_GATEWAY_KEY, and DBQ_DATABASE. No portal credential is
stored in source control, returned to the browser, or written to logs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_LIMIT = 200
_MAX_OFFSET = 100_000
_DEFAULT_DATABASE = "anuoluwatide9db"
_SEMESTERS = {"1st", "2nd", "3rd", "4th", "-"}

# Exact descriptions carried over from the uploaded DBQ tool for core portal tables.
_TABLE_PURPOSE_OVERRIDES: dict[str, tuple[str, str]] = {
    "studenttb": ("Student master records", "Student identity, programme, entry session, category, contact and active-status data."),
    "studenttb_status_history": ("Student status history", "Historical changes to student status and reasons for those changes."),
    "regtb": ("Live course registrations and results", "Student-course-session-semester rows, encoded scores, grades, operators and publication state."),
    "tem_regtb": ("Registration/result staging", "Temporary rows prepared before promotion to the live result table."),
    "regtb_history": ("Registration/result history", "Historical registration-row snapshots and recorded changes."),
    "regtb_reupload": ("Result re-upload work queue", "Rows and metadata associated with result re-upload or correction processing."),
    "coursetb": ("Master course catalogue", "Course code, title, units and programme/department classification."),
    "deptcoursetb": ("Session curriculum catalogue", "Programme/department curriculum membership, level, semester, session and units."),
    "course_allocationtb": ("Course upload allocation and approval", "Course allocations, file numbers, approvals, publication and upload metadata."),
    "files_uploadtb": ("Uploaded result-file registry", "Uploaded input/output files, course, session, purpose, operator and batch metadata."),
    "result_upload_datetb": ("Result release schedule", "Session/semester result-release dates or publication windows."),
    "settings_gradtb": ("Grading configuration", "Programme/level/session grading bands or grade-point settings."),
    "chargestb": ("Fee charge rules", "Session, category, student status, level, programme and amount rules."),
    "chargestb_detail": ("Fee charge line items", "Detailed fee components linked to charge groups and student contexts."),
    "charges_itemtb": ("Fee item catalogue", "Individual charge-item codes and descriptions."),
    "charges_typetb": ("Fee group catalogue", "Charge-group/folio definitions used to organize fee components."),
    "portal_general_payment_setup": ("General payment setup", "Active payment items, fee codes, names, amounts, categories, beneficiaries and expiry dates."),
    "settings_paymenttb": ("Payment settings", "Payment parameters, bank/payment-item settings, amount rules and session controls."),
    "portal_paymenttb": ("Main payment ledger", "Student receipts, amounts, RRR, transaction IDs, fee codes, responses, status and sessions."),
    "portal_payment_detailtb": ("Payment detail ledger", "Per-folio payment allocations reconciled to the main payment amount."),
    "audit_logs_payment": ("Payment audit log", "Manual or system payment INSERT/UPDATE/DELETE audit events."),
    "payment_attempttb": ("Payment attempts", "Payment initiation data before final success or failure."),
    "payment_split_definitiontb": ("Payment split definitions", "Rules for distributing payments across beneficiaries or charge components."),
    "payment_split_logstb": ("Payment split logs", "Recorded payment-split allocations and processing outcomes."),
    "trans_payment_split_logstb": ("Legacy payment split transactions", "Historical or migrated payment-split transaction records."),
    "exceptiontb_payment": ("Payment exceptions", "Payment exceptions, overrides and failed reconciliation cases."),
    "portal_general_payment_setup_02092025": ("Archived payment setup", "Historical snapshot of payment configuration; normally read-only."),
    "course_evaluationtb": ("Course evaluation records", "Student/course evaluation submissions and metadata."),
    "stafftb": ("Staff master records", "Staff identity, department, employment and account identifiers."),
    "staff_roletb": ("Staff role assignments", "Staff roles and portal permissions."),
    "staff_emailtb": ("Staff email directory", "Staff email addresses and communication metadata."),
    "trans_users": ("Portal user transactions", "User/account transaction records used by identity workflows."),
    "trans_portal_logstb": ("Portal activity log", "Historical portal actions, requests and user activity events."),
    "trans_orders": ("Transaction orders", "Payment/order records and order-level status."),
    "trans_regtb": ("Historical registration transactions", "Migrated or historical registration transactions."),
    "trans_studenttb": ("Historical student transactions", "Migrated or historical student records."),
}


def table_purpose(table_name: str, columns: list[str]) -> dict[str, str]:
    if table_name in _TABLE_PURPOSE_OVERRIDES:
        purpose, handles = _TABLE_PURPOSE_OVERRIDES[table_name]
        return {"purpose": purpose, "handles": handles}
    lower = table_name.lower()
    if any(token in lower for token in ("backup", "_old", "_copy", "_bk", "history")):
        return {"purpose": "Archive or historical snapshot", "handles": "Older, copied or historical data; normally read-only."}
    if "audit" in lower:
        return {"purpose": "Audit and accountability log", "handles": "Who changed what and when."}
    if any(token in lower for token in ("payment", "charge", "fee")):
        return {"purpose": "Payment or fee subsystem table", "handles": "Payments, fees, charges, beneficiaries or reconciliation."}
    if any(token in lower for token in ("upload", "result", "grade")):
        return {"purpose": "Result upload or publication table", "handles": "Result files, grades, publication state or release metadata."}
    if "student" in lower:
        return {"purpose": "Student information table", "handles": "Student identity, status, programme or contact workflow data."}
    if any(token in lower for token in ("staff", "user", "role")):
        return {"purpose": "Staff, user or access table", "handles": "Staff identity, accounts, roles or permissions."}
    if any(token in lower for token in ("course", "curriculum", "allocation")):
        return {"purpose": "Academic course or curriculum table", "handles": "Course catalogue, curriculum membership or allocation."}
    if any(token in lower for token in ("reg", "enrol")):
        return {"purpose": "Registration table", "handles": "Registration, enrolment or course-selection data."}
    if any(token in lower for token in ("transcript", "certificate")):
        return {"purpose": "Transcript and certification table", "handles": "Transcript, certificate or statement workflow."}
    if lower.startswith("trans"):
        return {"purpose": "Transaction or migration table", "handles": "Historical, transactional or migrated records."}
    if any(token in lower for token in ("setting", "config", "parameter")):
        return {"purpose": "Portal configuration table", "handles": "Settings, parameters and feature configuration."}
    if "log" in lower:
        return {"purpose": "Operational log table", "handles": "Events, actions and system activity."}
    if any(token in lower for token in ("admission", "application")):
        return {"purpose": "Admissions and application table", "handles": "Applicant and admission workflow data."}
    if any(token in lower for token in ("bank", "beneficiar")):
        return {"purpose": "Banking or beneficiary table", "handles": "Bank, beneficiary and settlement details."}
    if any(token in lower for token in ("hostel", "accommodation")):
        return {"purpose": "Accommodation table", "handles": "Hostel, room and accommodation records."}
    if any(token in lower for token in ("invoice", "receipt", "order")):
        return {"purpose": "Billing and order table", "handles": "Invoices, receipts, orders and settlement states."}
    if any(token in lower for token in ("news", "notice", "event", "banner")):
        return {"purpose": "Portal content table", "handles": "News, notices, events and public content."}
    if any(token in lower for token in ("programme", "program", "major", "specialization")):
        return {"purpose": "Programme structure table", "handles": "Academic programmes and programme metadata."}
    column_names = {column.lower() for column in columns}
    if {"regno", "course_code"}.issubset(column_names):
        return {"purpose": "Student academic record table", "handles": "Student-course academic records linked by registration and course code."}
    if {"amount", "session"}.issubset(column_names):
        return {"purpose": "Sessional financial table", "handles": "Session-specific charges, payments or financial transactions."}
    if "regno" in column_names:
        return {"purpose": "Student-linked workflow table", "handles": "Workflow or transaction records linked to a student."}
    return {"purpose": "Portal reference or module table", "handles": "Module-specific operational or reference data; inspect fields and keys before writing."}


def column_role(column_name: str) -> str:
    name = column_name.lower()
    exact = {
        "id": "Record identifier.", "regno": "Student registration identifier.", "course_code": "Course identifier.",
        "title": "Course or item title.", "unit": "Credit-unit value.", "session": "Academic session.",
        "semester": "Academic semester.", "level": "Study level.", "prog_id": "Programme identifier.",
        "dept_id": "Department/faculty identifier.", "portal_category_code": "Portal category partition.",
        "amount": "Monetary amount.", "fee_amount": "Configured fee amount.", "rrr": "Payment reference.",
        "receipt_number": "Portal receipt identifier.", "trans_id": "Transaction identifier.",
        "payment_status": "Payment completion state.", "response_code": "Gateway response code.",
        "upload_status": "Result publication flag.", "data_upload_status": "Secondary upload marker.",
        "final_ca": "Encoded continuous-assessment score.", "final_exam": "Encoded examination score.",
        "ca": "Legacy/raw CA score.", "exam": "Legacy/raw examination score.",
        "ca_entry_by": "CA score-entry operator.", "exam_entry_by": "Exam score-entry operator.",
        "reg_entry_by": "Registration-row operator.", "modify_by": "Last modification operator.",
        "entry_by": "Record creation/operator identifier.",
    }
    if name in exact:
        return exact[name]
    if "status" in name:
        return "Status or workflow flag."
    if "date" in name or name.endswith("_at") or "time" in name:
        return "Date/time lifecycle field."
    if any(token in name for token in ("amount", "price", "cost", "charge")):
        return "Financial amount or charge value."
    if "code" in name or name.endswith("_id"):
        return "Reference/code linking related data."
    if any(token in name for token in ("name", "title", "desc", "address")):
        return "Human-readable descriptive field."
    if any(token in name for token in ("file", "upload")):
        return "File or upload metadata."
    if any(token in name for token in ("user", "staff", "operator", "by")):
        return "User, staff or operator attribution."
    return "Table-specific field; inspect type and keys before editing."


class DBQError(RuntimeError):
    """Base error for safe admin database operations."""


class DBQConfigError(DBQError):
    """Raised when the gateway is not configured securely."""


class DBQValidationError(DBQError):
    """Raised when an admin operation contains invalid identifiers or values."""


def _database() -> str:
    value = os.getenv("DBQ_DATABASE", _DEFAULT_DATABASE).strip()
    return _identifier(value, "database")


def _identifier(value: Any, name: str = "identifier") -> str:
    text = str(value or "").strip()
    if not _TOKEN_RE.fullmatch(text):
        raise DBQValidationError(f"Invalid {name}")
    return text


def _table(value: Any) -> str:
    return _identifier(value, "table name")


def _column(value: Any) -> str:
    return _identifier(value, "column name")


def _quote_identifier(value: str) -> str:
    return f"`{_identifier(value)}`"


def sql_lit(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _gateway_url() -> str:
    value = (os.getenv("DBQ_GATEWAY_URL") or os.getenv("UNIABUJA_DBQ_URL") or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise DBQConfigError("DBQ gateway URL is not configured as a secure HTTPS URL")
    return value


def _gateway_key() -> str:
    value = (os.getenv("DBQ_GATEWAY_KEY") or os.getenv("UNIABUJA_DBQ_KEY") or "").strip()
    if not value:
        raise DBQConfigError("DBQ gateway key is not configured")
    return value


def sql(query: str, *, timeout: float = 60.0) -> Any:
    if not isinstance(query, str) or not query.strip() or len(query) > 64_000:
        raise DBQValidationError("SQL request is empty or too large")
    if not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 120:
        raise DBQValidationError("SQL timeout must be between 1 and 120 seconds")
    try:
        response = httpx.post(
            _gateway_url(),
            data={"sql": query},
            headers={"Accept": "application/json", "X-DBQ-Key": _gateway_key()},
            timeout=float(timeout),
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise DBQError(f"Database gateway returned HTTP {response.status_code}")
        body = response.json()
    except DBQError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise DBQError(f"Database gateway request failed: {type(exc).__name__}") from exc
    if isinstance(body, dict) and body.get("error"):
        raise DBQError("Database gateway rejected the SQL request")
    if isinstance(body, (list, dict)):
        return body
    return [{"raw": str(body)[:300]}]


_SENSITIVE_COLUMN_NAMES = {
    "password", "password_hash", "passphrase", "secret", "token", "access_token",
    "refresh_token", "private_key", "api_key",
}


def _is_sensitive_column(name: Any) -> bool:
    normalized = str(name or "").strip().lower()
    if normalized in {"password_length", "password_format"}:
        return False
    return normalized in _SENSITIVE_COLUMN_NAMES or "password" in normalized or normalized.endswith("_token")


def _redact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): "[REDACTED]" if _is_sensitive_column(key) else value for key, value in row.items()}


def _rows(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DBQError(f"{context} did not return rows")
    return [_redact_row(row) for row in value if isinstance(row, Mapping)]


def _schema_rows(table: str) -> list[dict[str, Any]]:
    table = _table(table)
    result = sql(
        "SELECT TABLE_NAME,ORDINAL_POSITION,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,"
        "COLUMN_DEFAULT,COLUMN_KEY,EXTRA,COLUMN_COMMENT "
        f"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA={sql_lit(_database())} "
        f"AND TABLE_NAME={sql_lit(table)} ORDER BY ORDINAL_POSITION"
    )
    rows = _rows(result, "Table schema")
    if not rows:
        raise DBQValidationError("Table was not found")
    return rows


def _primary_columns(schema: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("COLUMN_NAME")) for row in schema if row.get("COLUMN_KEY") == "PRI"]


def ping() -> dict[str, Any]:
    result = sql("SELECT 1 AS dbq_ok")
    return {"ok": True, "operation": "dbq_ping", "result": result}


def catalog(search: str = "") -> dict[str, Any]:
    table_rows = _rows(sql(
        "SELECT TABLE_NAME,TABLE_TYPE,ENGINE,TABLE_ROWS,CREATE_TIME,UPDATE_TIME,TABLE_COMMENT "
        f"FROM information_schema.TABLES WHERE TABLE_SCHEMA={sql_lit(_database())} ORDER BY TABLE_NAME"
    ), "Table catalogue")
    column_rows = _rows(sql(
        "SELECT TABLE_NAME,ORDINAL_POSITION,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,COLUMN_KEY,EXTRA,COLUMN_COMMENT "
        f"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA={sql_lit(_database())} ORDER BY TABLE_NAME,ORDINAL_POSITION"
    ), "Column catalogue")
    key_rows = _rows(sql(
        "SELECT TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,ORDINAL_POSITION,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME "
        f"FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA={sql_lit(_database())} ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION"
    ), "Key catalogue")
    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    keys_by_table: dict[str, list[dict[str, Any]]] = {}
    for row in column_rows:
        columns_by_table.setdefault(str(row.get("TABLE_NAME")), []).append(row)
    for row in key_rows:
        keys_by_table.setdefault(str(row.get("TABLE_NAME")), []).append(row)
    records = []
    needle = str(search or "").strip().lower()
    for row in table_rows:
        name = str(row.get("TABLE_NAME") or "")
        cols = columns_by_table.get(name, [])
        purpose = table_purpose(name, [str(col.get("COLUMN_NAME") or "") for col in cols])
        keys = keys_by_table.get(name, [])
        searchable = json.dumps({**row, **purpose, "columns": cols}, default=str).lower()
        if needle and needle not in searchable:
            continue
        primary = [str(col.get("COLUMN_NAME")) for col in cols if col.get("COLUMN_KEY") == "PRI"]
        records.append({
            **row,
            "purpose": purpose["purpose"],
            "handles": purpose["handles"],
            "columns": [{**col, "role": column_role(str(col.get("COLUMN_NAME") or ""))} for col in cols],
            "keys": keys,
            "primary_columns": primary,
            "can_update_delete": bool(primary),
            "write_guidance": "Inspect schema and primary key before writing; archived, audit and log tables should normally be read-only." if any(token in name.lower() for token in ("archive", "history", "audit", "log", "old", "copy")) else "Use the guarded admin editor and verify the returned row after each write.",
        })
    return {"database": _database(), "table_count": len(records), "inventory_scope": "All information_schema.TABLES rows in the configured portal database", "tables": records}


def portal_workspaces(search: str = "") -> dict[str, Any]:
    inventory = catalog(search)
    groups: dict[str, dict[str, Any]] = {
        "students": {"title": "Students", "description": "Student master, status, registration, enrolment and student-linked workflows.", "tables": []},
        "staff_and_roles": {"title": "Staff, HOD, dean and roles", "description": "Staff directory, role assignments, departments, faculties, HOD/dean or academic-administration records discovered in the live schema.", "tables": []},
        "courses_and_curriculum": {"title": "Courses and curriculum", "description": "Course catalogue, programmes, departments, curriculum and allocations.", "tables": []},
        "grades_and_results": {"title": "Grades and results", "description": "Registration rows, grade/result tables, uploads, publication, transcripts and evaluation workflows.", "tables": []},
        "payments_and_finance": {"title": "Payments and finance", "description": "Payment, fee, charge, receipt, order, banking and reconciliation records.", "tables": []},
        "other_portal_tables": {"title": "Other portal tables", "description": "All remaining information_schema tables not classified above.", "tables": []},
    }
    for table in inventory["tables"]:
        name = str(table.get("TABLE_NAME") or "").lower()
        purpose = f"{table.get('purpose', '')} {table.get('handles', '')}".lower()
        if any(token in name or token in purpose for token in ("staff", "role", "hod", "dean", "faculty", "department", "academic administration")):
            group = "staff_and_roles"
        elif name in {"regtb", "tem_regtb", "regtb_history", "regtb_reupload", "trans_regtb"} or any(token in name for token in ("result", "grade", "transcript", "evaluation")):
            group = "grades_and_results"
        elif any(token in name or token in purpose for token in ("course", "curriculum", "programme", "program", "allocation")):
            group = "courses_and_curriculum"
        elif any(token in name or token in purpose for token in ("student", "enrol")):
            group = "students"
        elif any(token in name or token in purpose for token in ("payment", "charge", "fee", "receipt", "order", "bank", "beneficiar", "finance")):
            group = "payments_and_finance"
        else:
            group = "other_portal_tables"
        groups[group]["tables"].append({
            "name": table.get("TABLE_NAME"), "purpose": table.get("purpose"), "handles": table.get("handles"),
            "primary_columns": table.get("primary_columns", []), "can_update_delete": table.get("can_update_delete", False),
            "write_guidance": table.get("write_guidance", ""),
        })
    return {"database": inventory["database"], "table_count": inventory["table_count"], "workspaces": list(groups.values())}


def schema(table: str) -> dict[str, Any]:
    table = _table(table)
    columns = _schema_rows(table)
    keys = _rows(
        sql(
            "SELECT TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,ORDINAL_POSITION,"
            "REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME "
            f"FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA={sql_lit(_database())} "
            f"AND TABLE_NAME={sql_lit(table)} ORDER BY CONSTRAINT_NAME,ORDINAL_POSITION"
        ),
        "Table keys",
    )
    purpose = table_purpose(table, [str(row.get("COLUMN_NAME") or "") for row in columns])
    return {"table": table, "columns": [{**row, "role": column_role(str(row.get("COLUMN_NAME") or ""))} for row in columns], "keys": keys, "primary_columns": _primary_columns(columns), "purpose": purpose["purpose"], "handles": purpose["handles"], "can_update_delete": bool(_primary_columns(columns))}


def read_table(table: str, limit: Any = 50, offset: Any = 0) -> dict[str, Any]:
    table = _table(table)
    _schema_rows(table)
    try:
        limit_i = max(1, min(_MAX_LIMIT, int(limit)))
        offset_i = max(0, min(_MAX_OFFSET, int(offset)))
    except (TypeError, ValueError) as exc:
        raise DBQValidationError("limit and offset must be integers") from exc
    rows = _rows(sql(f"SELECT * FROM {_quote_identifier(table)} LIMIT {limit_i} OFFSET {offset_i}"), "Table read")
    return {"table": table, "limit": limit_i, "offset": offset_i, "rows": rows}


def _searchable_columns(schema_rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in schema_rows:
        name = _column(row.get("COLUMN_NAME"))
        column_type = str(row.get("COLUMN_TYPE") or "").lower()
        if any(kind in column_type for kind in ("char", "text", "enum", "set", "json", "int", "decimal", "numeric", "float", "double", "date", "time")):
            columns.append(name)
    return columns[:24]


def _search_table_rows(table: str, term: str, limit: int) -> dict[str, Any]:
    table = _table(table)
    schema_rows = _schema_rows(table)
    columns = _searchable_columns(schema_rows)
    if not columns:
        return {"table": table, "search_columns": [], "rows": []}
    needle = str(term or "").strip()
    if len(needle) < 2 or len(needle) > 128:
        raise DBQValidationError("Search text must be between 2 and 128 characters")
    like = sql_lit(f"%{needle}%")
    predicate = " OR ".join(f"LOWER({_quote_identifier(column)}) LIKE LOWER({like})" for column in columns)
    rows = _rows(sql(
        f"SELECT * FROM {_quote_identifier(table)} WHERE {predicate} LIMIT {limit}"
    ), "Table search")
    return {"table": table, "search_columns": columns, "rows": rows}


def table_search(table: str, term: str, limit: Any = 50) -> dict[str, Any]:
    try:
        limit_i = max(1, min(100, int(limit)))
    except (TypeError, ValueError) as exc:
        raise DBQValidationError("search limit must be an integer") from exc
    result = _search_table_rows(table, term, limit_i)
    return {"operation": "table_search", "term": str(term or "").strip(), "limit": limit_i, **result}


def person_search(term: str, scope: str = "all", limit: Any = 50) -> dict[str, Any]:
    scope = str(scope or "all").strip().lower()
    if scope not in {"all", "students", "staff"}:
        raise DBQValidationError("person search scope must be all, students or staff")
    try:
        limit_i = max(1, min(100, int(limit)))
    except (TypeError, ValueError) as exc:
        raise DBQValidationError("search limit must be an integer") from exc
    needle = str(term or "").strip()
    if len(needle) < 2 or len(needle) > 128:
        raise DBQValidationError("Search text must be between 2 and 128 characters")
    table_names = {
        "students": ("studenttb",),
        "staff": ("stafftb", "staff_emailtb"),
        "all": ("studenttb", "stafftb", "staff_emailtb"),
    }[scope]
    matches: list[dict[str, Any]] = []
    for table_name in table_names:
        try:
            result = _search_table_rows(table_name, needle, limit_i)
        except DBQValidationError as exc:
            if str(exc) == "Table was not found":
                continue
            raise
        matches.append(result)
    return {"operation": "person_search", "term": needle, "scope": scope, "limit": limit_i, "matches": matches}


def batch_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    course = str(payload.get("course") or "").strip().upper()
    session = str(payload.get("session") or "").strip()
    semester = str(payload.get("semester") or "").strip()
    if not course or not session:
        raise DBQValidationError("course and session are required")
    if semester and semester not in _SEMESTERS:
        raise DBQValidationError("invalid semester")
    conditions = [f"course_code={sql_lit(course)}", f"session={sql_lit(session)}"]
    if semester:
        conditions.append(f"semester={sql_lit(semester)}")
    where = " AND ".join(conditions)
    return {
        "course": course, "session": session, "semester": semester,
        "row_counts": _rows(sql(
            "SELECT upload_status,ca_entry_by,exam_entry_by,COUNT(*) AS rows_count,MIN(added_date) AS first_added,MAX(added_date) AS last_added "
            f"FROM regtb WHERE {where} GROUP BY upload_status,ca_entry_by,exam_entry_by ORDER BY upload_status,rows_count DESC"
        ), "Batch score counts"),
        "unpublished": _rows(sql(
            "SELECT regno,course_code,semester,unit,status,ca,exam,score_decode(final_ca) AS decoded_ca,score_decode(final_exam) AS decoded_exam,upload_status,ca_entry_by,exam_entry_by,reg_entry_by,added_date,added_time "
            f"FROM regtb WHERE {where} AND upload_status='No' ORDER BY added_date,added_time,regno"
        ), "Unpublished score rows"),
        "upload_files": _rows(sql(
            "SELECT fileno,action_date,action_time,course_code,input_file,output_file,session,purpose,portal_category_code FROM files_uploadtb "
            f"WHERE course_code={sql_lit(course)} AND session={sql_lit(session)} ORDER BY action_date DESC,action_time DESC"
        ), "Upload file log"),
        "allocations": _rows(sql(
            "SELECT stud_dept_id,course_code,session,semester,fileno,dept_approved_by,fact_approved_by,published_by,published_date,published_time,upload_count,upload_date,upload_time,entry_by,portal_category_code FROM course_allocationtb "
            f"WHERE course_code={sql_lit(course)} AND session={sql_lit(session)}"
        ), "Course allocations"),
    }


def map_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    course = str(payload.get("course") or "").strip().upper()
    session = str(payload.get("session") or "").strip()
    semester = str(payload.get("semester") or "").strip()
    prog = str(payload.get("prog") or "ELC").strip()
    category = str(payload.get("portalCategory") or "ug").strip()
    if not course or not session or semester not in _SEMESTERS or not prog or not category:
        raise DBQValidationError("course, session, valid semester, programme and portal category are required")
    return {
        "course": course, "session": session, "semester": semester, "prog": prog, "portal_category": category,
        "master_course": _rows(sql(
            "SELECT course_code,title,unit,semester,level,dept_id,prog_id,portal_category_code FROM coursetb "
            f"WHERE course_code={sql_lit(course)} AND portal_category_code={sql_lit(category)}"
        ), "Master course"),
        "session_curriculum": _rows(sql(
            "SELECT course_code,title,unit,semester,level,prog_id,session,portal_category_code FROM deptcoursetb "
            f"WHERE course_code={sql_lit(course)} AND prog_id={sql_lit(prog)} AND session={sql_lit(session)} AND portal_category_code={sql_lit(category)}"
        ), "Session curriculum"),
        "allocations": _rows(sql(
            "SELECT stud_dept_id,course_code,session,semester,fileno,dept_approved_by,fact_approved_by,published_by,published_date,published_time,upload_date,upload_time,entry_by,portal_category_code FROM course_allocationtb "
            f"WHERE course_code={sql_lit(course)} AND session={sql_lit(session)} AND semester={sql_lit(semester)} AND portal_category_code={sql_lit(category)}"
        ), "Course map allocation"),
    }


def student_lookup(regno: str, session: str = "") -> dict[str, Any]:
    regno = str(regno or "").strip()
    session = str(session or "").strip()
    if not regno or len(regno) > 128:
        raise DBQValidationError("registration number is required")
    # Registration numbers are stored in the portal in a canonical form. Use a
    # direct equality predicate so MySQL can use the regno index; wrapping the
    # indexed column in LOWER() caused the live lookup to scan and time out.
    student_conditions = f"regno={sql_lit(regno)}"
    student = _rows(
        sql(
            "SELECT regno,surname,first_name,other_name,prog_id,session_of_entry,status,portal_category_code "
            f"FROM studenttb WHERE {student_conditions} LIMIT 1",
            timeout=20.0,
        ),
        "Student lookup",
    )
    result_conditions = [student_conditions]
    if session:
        result_conditions.append(f"session={sql_lit(session)}")
    result_error = None
    try:
        # Keep the indexed regno predicate while returning decoded values for the
        # administrator. Raw storage columns remain available for audit, but the
        # UI receives decoded_ca/decoded_exam for ordinary score entry.
        results = _rows(
            sql(
                "SELECT regno,course_code,title,unit,semester,status,session,regstatus,"
                "score_decode(final_ca) AS decoded_ca,score_decode(final_exam) AS decoded_exam,"
                "final_ca,final_exam,ca,exam,upload_status,ca_entry_by,exam_entry_by,"
                "reg_entry_by,modify_by "
                f"FROM regtb WHERE {' AND '.join(result_conditions)} "
                "ORDER BY session DESC,semester,course_code",
                timeout=20.0,
            ),
            "Student result lookup",
        )
    except DBQError as exc:
        results = []
        result_error = str(exc)
    response: dict[str, Any] = {"student": student[0] if student else None, "results": results}
    if result_error:
        response["result_status"] = "unavailable"
        response["result_error"] = result_error
    else:
        response["result_status"] = "loaded"
    return response


def _row_where(keys: Mapping[str, Any], primary: list[str]) -> str:
    if not primary:
        raise DBQValidationError("Table has no primary key; generic update/delete is disabled")
    missing = [name for name in primary if name not in keys]
    if missing:
        raise DBQValidationError(f"Primary key values are required: {', '.join(missing)}")
    return " AND ".join(f"{_quote_identifier(name)}={sql_lit(keys[name])}" for name in primary)


def _affected(result: Any) -> int | None:
    if isinstance(result, dict):
        for key in ("affected_rows", "affectedRows", "rowCount", "ok"):
            if key in result:
                try:
                    return int(result[key])
                except (TypeError, ValueError):
                    return None
    return None


def generic_insert(table: str, values: Mapping[str, Any]) -> dict[str, Any]:
    table = _table(table)
    schema_rows = _schema_rows(table)
    allowed = {_column(row.get("COLUMN_NAME")) for row in schema_rows}
    clean = {_column(key): value for key, value in values.items() if _column(key) in allowed}
    if any(_is_sensitive_column(key) for key in clean):
        raise DBQValidationError("Password or secret fields require a dedicated admin operation")
    if not clean:
        raise DBQValidationError("Insert values contain no valid table columns")
    columns = ",".join(_quote_identifier(key) for key in clean)
    values_sql = ",".join(sql_lit(value) for value in clean.values())
    result = sql(f"INSERT INTO {_quote_identifier(table)} ({columns}) VALUES ({values_sql})")
    return {"ok": True, "table": table, "affected_rows": _affected(result), "result": result}


def generic_update(table: str, keys: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    table = _table(table)
    schema_rows = _schema_rows(table)
    columns = {_column(row.get("COLUMN_NAME")) for row in schema_rows}
    primary = _primary_columns(schema_rows)
    where = _row_where(keys, primary)
    clean = {_column(key): value for key, value in updates.items() if _column(key) in columns and _column(key) not in primary}
    if any(_is_sensitive_column(key) for key in clean):
        raise DBQValidationError("Password or secret fields require a dedicated admin operation")
    if not clean:
        raise DBQValidationError("Update values contain no non-key table columns")
    assignments = ",".join(f"{_quote_identifier(key)}={sql_lit(value)}" for key, value in clean.items())
    before_rows = _rows(sql(f"SELECT * FROM {_quote_identifier(table)} WHERE {where} LIMIT 1"), "Table update lookup")
    if len(before_rows) != 1:
        raise DBQError("Update target was not found uniquely")
    result = sql(f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {where}")
    after_rows = _rows(sql(f"SELECT * FROM {_quote_identifier(table)} WHERE {where} LIMIT 1"), "Table update verification")
    if len(after_rows) != 1:
        raise DBQError("Updated row could not be verified")
    return {"ok": True, "table": table, "affected_rows": _affected(result), "before": before_rows[0], "after": after_rows[0], "result": result}


def student_account_lookup(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno = str(payload.get("regno") or "").strip()
    if not regno or len(regno) > 128:
        raise DBQValidationError("registration number is required")
    rows = _rows(sql(
        "SELECT regno,surname,first_name,other_name,email,tel_no,status,online_status,last_login_date,last_login_time "
        f"FROM studenttb WHERE regno={sql_lit(regno)} LIMIT 1",
    ), "Student account lookup")
    return {"ok": True, "operation": "student_account_lookup", "student": rows[0] if len(rows) == 1 else None}


def _password_storage_format(stored: str) -> str:
    value = stored.strip()
    lowered = value.lower()
    if not value:
        return "plain"
    if re.fullmatch(r"[0-9a-f]{32}", lowered):
        return "md5"
    if re.fullmatch(r"[0-9a-f]{40}", lowered):
        return "sha1"
    if re.fullmatch(r"[0-9a-f]{64}", lowered):
        return "sha256"
    if lowered.startswith(("$2a$", "$2b$", "$2y$")):
        return "bcrypt"
    if lowered.startswith(("$p$", "$h$")):
        return "phpass"
    # The supplied portal PHP implementation stores SHA-512(password + salt)
    # followed by MD5(password + salt), both as lowercase hex.
    if re.fullmatch(r"[0-9a-f]{160}", lowered):
        return "sha512_md5_uapro"
    return "plain"


def _encode_student_password(password: str, storage_format: str) -> str:
    raw = password.encode("utf-8")
    if storage_format == "md5":
        return hashlib.md5(raw, usedforsecurity=False).hexdigest()
    if storage_format == "sha1":
        return hashlib.sha1(raw, usedforsecurity=False).hexdigest()
    if storage_format == "sha256":
        return hashlib.sha256(raw).hexdigest()
    if storage_format == "sha512_md5_uapro":
        salted = password.encode("utf-8") + b"uapro"
        return hashlib.sha512(salted).hexdigest() + hashlib.md5(salted, usedforsecurity=False).hexdigest()
    if storage_format == "bcrypt":
        try:
            import bcrypt
        except ImportError:
            raise DBQError("Student password uses bcrypt but bcrypt support is unavailable") from None
        return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")
    if storage_format == "phpass":
        raise DBQError("Student password uses an unsupported legacy hash format")
    return password


def student_password_format_diagnostics(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno = str(payload.get("regno") or "").strip()
    prefix = str(payload.get("prefix") or "").strip()
    if regno and prefix:
        raise DBQValidationError("Use either regno or prefix, not both")
    if not regno and not prefix:
        raise DBQValidationError("regno or prefix is required")
    if len(regno or prefix) > 128:
        raise DBQValidationError("regno or prefix is too long")
    where = f"regno={sql_lit(regno)}" if regno else f"regno LIKE {sql_lit(prefix + '%')}"
    format_case = (
        "CASE WHEN password IS NULL OR password='' THEN 'empty' "
        "WHEN password REGEXP '^[0-9A-Fa-f]{32}$' THEN 'md5' "
        "WHEN password REGEXP '^[0-9A-Fa-f]{40}$' THEN 'sha1' "
        "WHEN password REGEXP '^[0-9A-Fa-f]{64}$' THEN 'sha256' "
        "WHEN password REGEXP '^\\$2[aby]\\$' THEN 'bcrypt' "
        "WHEN password REGEXP '^[0-9A-Fa-f]{160}$' THEN 'sha512_md5_uapro' ELSE 'plain_or_other' END"
    )
    sample = _rows(sql(
        "SELECT regno,CHAR_LENGTH(COALESCE(password,'')) AS password_length,"
        f"{format_case} AS password_format FROM studenttb WHERE {where} LIMIT 50",
    ), "Student password diagnostics")
    trans_where = f"username={sql_lit(regno)}" if regno else f"username LIKE {sql_lit(prefix + '%')}"
    trans_users = _rows(sql(
        "SELECT username,CHAR_LENGTH(COALESCE(password,'')) AS password_length,"
        "status,"
        f"{format_case} AS password_format FROM trans_users WHERE {trans_where} LIMIT 50",
    ), "Transcript-user password diagnostics")
    sample_formats = sorted({str(row.get("password_format")) for row in sample + trans_users})
    return {
        "ok": True,
        "operation": "student_password_format_diagnostics",
        "studenttb": sample,
        "trans_users": trans_users,
        "observed_formats": sample_formats,
    }


def _dominant_student_password_format() -> str:
    rows = sql(
        "SELECT COUNT(*) AS total_count, "
        "SUM(CASE WHEN password REGEXP '^[0-9A-Fa-f]{32}$' THEN 1 ELSE 0 END) AS md5_count, "
        "SUM(CASE WHEN password REGEXP '^[0-9A-Fa-f]{40}$' THEN 1 ELSE 0 END) AS sha1_count, "
        "SUM(CASE WHEN password REGEXP '^[0-9A-Fa-f]{64}$' THEN 1 ELSE 0 END) AS sha256_count, "
        "SUM(CASE WHEN password REGEXP '^[0-9A-Fa-f]{160}$' THEN 1 ELSE 0 END) AS sha512_md5_uapro_count, "
        "SUM(CASE WHEN password REGEXP '^\\$2[aby]\\$' THEN 1 ELSE 0 END) AS bcrypt_count "
        "FROM studenttb WHERE password IS NOT NULL AND password <> ''",
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return "plain"
    row = rows[0]
    counts = {
        name: max(0, int(row.get(f"{name}_count") or 0))
        for name in ("md5", "sha1", "sha256", "sha512_md5_uapro", "bcrypt")
    }
    total = max(0, int(row.get("total_count") or 0))
    storage_format, count = max(counts.items(), key=lambda item: item[1])
    if count < 3 or count * 2 < total:
        return "plain"
    return storage_format


def _student_password_matches(password: str, stored: str, storage_format: str) -> bool:
    if storage_format == "bcrypt":
        try:
            import bcrypt
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("ascii"))
        except (ImportError, ValueError, UnicodeEncodeError):
            return False
    return hmac.compare_digest(stored, _encode_student_password(password, storage_format))


def student_password_reset(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno = str(payload.get("regno") or "").strip()
    new_password = str(payload.get("newPassword") or "")
    confirmation = str(payload.get("confirmPassword") or "")
    operator = str(payload.get("operator") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not regno or len(regno) > 128:
        raise DBQValidationError("registration number is required")
    if len(new_password) < 8 or len(new_password) > 128 or any(ord(char) < 0x20 for char in new_password):
        raise DBQValidationError("new password must be 8-128 characters without control characters")
    if new_password != confirmation:
        raise DBQValidationError("new password and confirmation do not match")
    if not operator or len(operator) > 64:
        raise DBQValidationError("operator is required")
    if not reason or len(reason) > 256:
        raise DBQValidationError("reset reason or ticket is required")
    if payload.get("confirmed") is not True:
        raise DBQValidationError("Password reset requires explicit confirmation")
    raw_students = sql(
        "SELECT regno,password FROM studenttb "
        f"WHERE regno={sql_lit(regno)} LIMIT 1",
    )
    if not isinstance(raw_students, list) or len(raw_students) != 1 or not isinstance(raw_students[0], Mapping):
        raise DBQError("Student account was not found uniquely")
    stored_password = str(raw_students[0].get("password") or "")
    storage_format = _password_storage_format(stored_password)
    format_source = "account"
    if storage_format == "plain" and stored_password:
        inferred_format = _dominant_student_password_format()
        if inferred_format != "plain":
            storage_format = inferred_format
            format_source = "student-table majority"
    stored_value = _encode_student_password(new_password, storage_format)
    result = sql(
        "UPDATE studenttb SET password="
        f"{sql_lit(stored_value)} WHERE regno={sql_lit(regno)}",
    )
    verified_rows = sql(
        "SELECT regno,password FROM studenttb "
        f"WHERE regno={sql_lit(regno)} LIMIT 1",
    )
    if not isinstance(verified_rows, list) or len(verified_rows) != 1 or not isinstance(verified_rows[0], Mapping):
        raise DBQError("Password reset was not verified because the student row could not be reloaded")
    stored_after = str(verified_rows[0].get("password") or "")
    if not _student_password_matches(new_password, stored_after, storage_format):
        raise DBQError("Password reset was not verified by the database; no success was reported")
    return {
        "ok": True,
        "operation": "student_password_reset",
        "regno": regno,
        "operator": operator,
        "reason": reason,
        "affected_rows": _affected(result),
        "password_format_source": format_source,
        "message": "Student password reset was written and verified by the database; the new password is not returned.",
    }


def generic_delete(table: str, keys: Mapping[str, Any], confirmed: Any) -> dict[str, Any]:
    if confirmed is not True:
        raise DBQValidationError("Delete requires explicit confirmation")
    table = _table(table)
    primary = _primary_columns(_schema_rows(table))
    where = _row_where(keys, primary)
    result = sql(f"DELETE FROM {_quote_identifier(table)} WHERE {where} LIMIT 1")
    return {"ok": True, "table": table, "affected_rows": _affected(result), "result": result}


def score_update(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno = str(payload.get("regno") or "").strip()
    course = str(payload.get("course") or "").strip().upper()
    session = str(payload.get("session") or "").strip()
    semester = str(payload.get("semester") or "").strip()
    try:
        ca = int(payload.get("ca"))
        exam = int(payload.get("exam"))
    except (TypeError, ValueError) as exc:
        raise DBQValidationError("CA and exam must be integers") from exc
    if not regno or not course or not session or semester not in {"1st", "2nd", "3rd", "4th", "-"}:
        raise DBQValidationError("regno, course, session and valid semester are required")
    if not 0 <= ca <= 100 or not 0 <= exam <= 100:
        raise DBQValidationError("CA and exam must be between 0 and 100")
    operator = str(payload.get("operator") or "").strip()
    modifier = str(payload.get("modifier") or operator).strip()
    if not operator or not modifier or len(operator) > 64 or len(modifier) > 64:
        raise DBQValidationError("operator and modifier are required")
    publish = bool(payload.get("publish"))
    where = (
        f"regno={sql_lit(regno)} AND course_code={sql_lit(course)} "
        f"AND session={sql_lit(session)} AND semester={sql_lit(semester)}"
    )
    before = _rows(sql(f"SELECT * FROM regtb WHERE {where}"), "Score lookup")
    if len(before) != 1:
        raise DBQError(f"Expected one score row, found {len(before)}")
    publication = ", upload_status='Yes'" if publish else ""
    query = (
        "UPDATE regtb SET final_ca=score_encode(" + str(ca) + "), "
        "final_exam=score_encode(" + str(exam) + "), ca=NULL, exam=NULL, "
        f"ca_entry_by={sql_lit(operator)},exam_entry_by={sql_lit(operator)},"
        f"modify_by={sql_lit(modifier)},data_upload_status=NULL{publication} WHERE {where}"
    )
    result = sql(query)
    after = _rows(sql(f"SELECT * FROM regtb WHERE {where}"), "Score verification")
    return {"ok": True, "operation": "score_update", "before": before[0], "after": after[0] if after else None, "affected_rows": _affected(result)}


def score_add(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno = str(payload.get("regno") or "").strip()
    course = str(payload.get("course") or "").strip().upper()
    session = str(payload.get("session") or "").strip()
    semester = str(payload.get("semester") or "").strip()
    title = str(payload.get("title") or "").strip()
    dept = str(payload.get("dept") or "").strip()
    prog = str(payload.get("prog") or "").strip()
    level = str(payload.get("level") or "").strip()
    operator = str(payload.get("operator") or "").strip()
    modifier = str(payload.get("modifier") or operator).strip()
    reg_entry_by = str(payload.get("regEntryBy") or operator).strip()
    status = str(payload.get("status") or "C").strip()
    regstatus = str(payload.get("regstatus") or "Normal").strip()
    fact_course = str(payload.get("factCourse") or "No").strip()
    portal_category = str(payload.get("portalCategory") or "ug").strip()
    try:
        unit = int(payload.get("unit"))
        ca = int(payload.get("ca"))
        exam = int(payload.get("exam"))
    except (TypeError, ValueError) as exc:
        raise DBQValidationError("unit, CA and exam must be integers") from exc
    if not all((regno, course, session, title, dept, prog, level, operator, modifier, reg_entry_by)):
        raise DBQValidationError("regno, course, session, title, dept, programme, level and operators are required")
    if semester not in _SEMESTERS or unit <= 0 or len(title) > 255 or fact_course not in {"Yes", "No"}:
        raise DBQValidationError("invalid semester, unit, title or fact-course flag")
    if not 0 <= ca <= 100 or not 0 <= exam <= 100:
        raise DBQValidationError("CA and exam must be between 0 and 100")
    existing = _rows(sql(
        "SELECT * FROM regtb WHERE "
        f"regno={sql_lit(regno)} AND course_code={sql_lit(course)} AND session={sql_lit(session)} AND semester={sql_lit(semester)}"
    ), "Score-row duplicate check")
    if existing:
        raise DBQValidationError("The result row already exists; use score update instead")
    publication = "Yes" if bool(payload.get("publish")) else "No"
    sql(
        "INSERT INTO regtb (regno,course_code,unit,status,semester,title,fact_course,dept_id,prog_id,level,session,regstatus,ca,exam,final_ca,final_exam,practical,upload_status,ca_entry_by,exam_entry_by,reg_entry_by,added_date,added_time,portal_category_code,modify_by,data_upload_status) VALUES ("
        f"{sql_lit(regno)},{sql_lit(course)},{unit},{sql_lit(status)},{sql_lit(semester)},{sql_lit(title)},{sql_lit(fact_course)},"
        f"{sql_lit(dept)},{sql_lit(prog)},{sql_lit(level)},{sql_lit(session)},{sql_lit(regstatus)},NULL,NULL,score_encode({ca}),score_encode({exam}),NULL,{sql_lit(publication)},"
        f"{sql_lit(operator)},{sql_lit(operator)},{sql_lit(reg_entry_by)},CURDATE(),CURTIME(),{sql_lit(portal_category)},{sql_lit(modifier)},NULL)"
    )
    after = _rows(sql(
        "SELECT * FROM regtb WHERE "
        f"regno={sql_lit(regno)} AND course_code={sql_lit(course)} AND session={sql_lit(session)} AND semester={sql_lit(semester)}"
    ), "Score-row verification")
    if len(after) != 1:
        raise DBQError("Inserted score row could not be verified uniquely")
    return {"ok": True, "operation": "score_add", "after": after[0]}


def score_publish(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno = str(payload.get("regno") or "").strip()
    course = str(payload.get("course") or "").strip().upper()
    session = str(payload.get("session") or "").strip()
    semester = str(payload.get("semester") or "").strip()
    modifier = str(payload.get("modifier") or "").strip()
    if not all((regno, course, session, modifier)) or semester not in {"1st", "2nd", "3rd", "4th", "-"}:
        raise DBQValidationError("regno, course, session, semester and modifier are required")
    where = f"regno={sql_lit(regno)} AND course_code={sql_lit(course)} AND session={sql_lit(session)} AND semester={sql_lit(semester)}"
    result = sql(f"UPDATE regtb SET upload_status='Yes',data_upload_status=NULL,modify_by={sql_lit(modifier)} WHERE {where}")
    return {"ok": True, "operation": "score_publish", "affected_rows": _affected(result)}


def _payment_identity(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    regno = str(payload.get("regno") or "").strip()
    session = str(payload.get("session") or "").strip()
    semester = str(payload.get("semester") or "").strip()
    if not regno or not session or semester not in {"1st", "2nd", "3rd", "4th", "-"}:
        raise DBQValidationError("registration number, session and valid semester are required")
    return regno, session, semester


def _payment_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    regno, session, semester = _payment_identity(payload)
    conditions = [
        f"regno={sql_lit(regno)}",
        f"session={sql_lit(session)}",
        f"semester={sql_lit(semester)}",
        "payment_desc='School Charges'",
    ]
    trans_id = str(payload.get("transId") or "").strip()
    if trans_id:
        conditions.append(f"trans_id={sql_lit(trans_id)}")
    return _rows(sql(
        "SELECT id,regno,amount,receipt_number,payment_desc,pay_item_id,payment_date,payment_time,"
        "response_code,response_desc,session,semester,prog_id,level,trans_id,entry_by,rrr,"
        "channel,fee_code,portal_category_code,payment_status FROM portal_paymenttb "
        f"WHERE {' AND '.join(conditions)} ORDER BY payment_date,payment_time,id"
    ), "Payment lookup")


def _payment_detail_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _rows(sql(
        "SELECT COUNT(*) AS detail_lines,COALESCE(SUM(CAST(amount AS DECIMAL(12,2))),0) AS detail_total,"
        "COUNT(DISTINCT pay_item_id) AS distinct_pay_items,MIN(pay_item_id) AS pay_item_id "
        "FROM portal_payment_detailtb "
        f"WHERE regno={sql_lit(row.get('regno'))} "
        f"AND session={sql_lit(row.get('session'))} AND trans_id={sql_lit(row.get('trans_id'))} "
        f"AND rrr={sql_lit(row.get('rrr'))}"
    ), "Payment detail lookup")
    return result[0] if result else {}


def payment_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = _payment_rows(payload)
    if len(rows) != 1:
        return {"operation": "payment_check", "payment_rows_found": len(rows), "rows": rows, "eligible": False}
    row = rows[0]
    detail = _payment_detail_summary(row)
    try:
        main_amount = Decimal(str(row.get("amount") or "0")).quantize(Decimal("0.01"))
        detail_amount = Decimal(str(detail.get("detail_total") or "0")).quantize(Decimal("0.01"))
    except Exception as exc:
        raise DBQError("Payment amount could not be parsed") from exc
    pay_item_matches = str(row.get("pay_item_id")) == str(detail.get("pay_item_id"))
    fee_matches = str(row.get("fee_code")) == str(row.get("pay_item_id"))
    eligible = bool(
        row.get("payment_status") == "Full" and row.get("response_code") == "01"
        and main_amount == detail_amount and pay_item_matches and fee_matches
        and str(row.get("fee_code")) not in {"", "0", "9999999999", "None"}
    )
    return {"operation": "payment_check", "row": row, "detail": detail, "eligible": eligible,
            "amount_matches": main_amount == detail_amount, "pay_item_matches": pay_item_matches,
            "fee_code_matches": fee_matches}


def payment_eligibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = payment_check(payload)
    return {"operation": "eligibility_check", "regno": payload.get("regno"),
            "session": payload.get("session"), "semester": payload.get("semester"),
            "registration_payment_gate": bool(result.get("eligible")), "payment": result}


def _checked_decimal(value: Any, name: str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception as exc:
        raise DBQValidationError(f"{name} must be a valid decimal amount") from exc
    if amount < 0:
        raise DBQValidationError(f"{name} cannot be negative")
    return amount


def payment_add(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno, session, semester = _payment_identity(payload)
    amount = _checked_decimal(payload.get("amount"), "amount")
    required = {"receipt": "receipt number", "rrr": "RRR", "transId": "transaction ID",
                "paymentDate": "payment date", "paymentTime": "payment time", "payItemId": "pay item ID",
                "feeCode": "fee code", "prog": "programme", "level": "level", "operator": "operator",
                "verificationRef": "verification reference"}
    values = {key: str(payload.get(key) or "").strip() for key in required}
    missing = [label for key, label in required.items() if not values[key]]
    if missing:
        raise DBQValidationError("Missing payment fields: " + ", ".join(missing))
    if values["feeCode"] in {"0", "9999999999"}:
        raise DBQValidationError("Do not use a placeholder fee code")
    details = payload.get("details")
    if not isinstance(details, list) or not details:
        raise DBQValidationError("At least one payment detail line is required")
    detail_rows = []
    detail_total = Decimal("0.00")
    for item in details:
        if not isinstance(item, Mapping) or not str(item.get("folio_code") or "").strip():
            raise DBQValidationError("Each payment detail needs folio_code and amount")
        item_amount = _checked_decimal(item.get("amount"), "detail amount")
        detail_rows.append({"folio_code": str(item["folio_code"]).strip(), "amount": item_amount})
        detail_total += item_amount
    if detail_total != amount:
        raise DBQValidationError(f"Payment detail total {detail_total:.2f} does not equal main amount {amount:.2f}")
    student = _rows(sql(
        "SELECT regno,prog_id,portal_category_code FROM studenttb "
        f"WHERE LOWER(regno)=LOWER({sql_lit(regno)}) LIMIT 1"
    ), "Student payment validation")
    if len(student) != 1:
        raise DBQError("Student was not found uniquely")
    expected_prog = values["prog"]
    expected_category = portal_category = str(payload.get("portalCategory") or "ug").strip()
    if str(student[0].get("prog_id")) != expected_prog:
        raise DBQValidationError("Programme does not match the student master record")
    if str(student[0].get("portal_category_code")) != expected_category:
        raise DBQValidationError("Portal category does not match the student master record")
    duplicate = _rows(sql(
        "SELECT COUNT(*) AS duplicate_count FROM portal_paymenttb "
        f"WHERE trans_id={sql_lit(values['transId'])} OR rrr={sql_lit(values['rrr'])} "
        f"OR receipt_number={sql_lit(values['receipt'])}"
    ), "Payment duplicate check")
    if duplicate and int(duplicate[0].get("duplicate_count", 0) or 0):
        raise DBQValidationError("Transaction, RRR, or receipt already exists")
    stud_category = str(payload.get("studCategory") or "Nigerian").strip()
    studstatus = str(payload.get("studstatus") or "Returning").strip()
    channel = str(payload.get("channel") or "Manual verified entry").strip()
    main_query = (
        "INSERT INTO portal_paymenttb (regno,amount,receipt_number,payment_desc,pay_item_id,payment_date,payment_time,"
        "response_code,response_desc,card_number,pay_ref,ret_ref,leadbank_cbncode,leadbank_name,session,semester,prog_id,level,"
        "trans_id,entry_by,rrr,channel,fee_code,portal_category_code,payment_status) VALUES ("
        f"{sql_lit(regno)},{sql_lit(f'{amount:.2f}')},{sql_lit(values['receipt'])},'School Charges',"
        f"{sql_lit(values['payItemId'])},{sql_lit(values['paymentDate'])},{sql_lit(values['paymentTime'])},"
        f"'01','Approved','','','','','',{sql_lit(session)},{sql_lit(semester)},{sql_lit(values['prog'])},"
        f"{sql_lit(values['level'])},{sql_lit(values['transId'])},{sql_lit(values['operator'])},{sql_lit(values['rrr'])},"
        f"{sql_lit(channel)},{sql_lit(values['feeCode'])},{sql_lit(portal_category)},'Full')"
    )
    inserted = False
    try:
        first = sql(main_query)
        inserted = True
        for item in detail_rows:
            item_amount_text = f"{item['amount']:.2f}"
            sql(
                "INSERT INTO portal_payment_detailtb (regno,folio_code,session,level,prog_id,stud_category,studstatus,"
                "portal_category_code,amount,entry_by,added_date,added_time,payment_date,payment_time,trans_id,rrr,pay_item_id) VALUES ("
                f"{sql_lit(regno)},{sql_lit(item['folio_code'])},{sql_lit(session)},{sql_lit(values['level'])},{sql_lit(values['prog'])},"
                f"{sql_lit(stud_category)},{sql_lit(studstatus)},{sql_lit(portal_category)},{sql_lit(item_amount_text)},"
                f"{sql_lit(values['operator'])},CURDATE(),CURTIME(),{sql_lit(values['paymentDate'])},{sql_lit(values['paymentTime'])},"
                f"{sql_lit(values['transId'])},{sql_lit(values['rrr'])},{sql_lit(values['payItemId'])})"
            )
        audit = json.dumps({"verification_ref": values["verificationRef"], "amount": f"{amount:.2f}", "rrr": values["rrr"], "receipt": values["receipt"]}, sort_keys=True)
        sql("INSERT INTO audit_logs_payment (table_name,record_id,action,changed_data,user,ip_address) VALUES ("
            f"'portal_paymenttb',{sql_lit(values['transId'])},'INSERT',{sql_lit(audit)},{sql_lit(values['operator'])},'admin-webui')")
    except Exception:
        if inserted:
            sql(f"DELETE FROM portal_payment_detailtb WHERE trans_id={sql_lit(values['transId'])} AND rrr={sql_lit(values['rrr'])}")
            sql(f"DELETE FROM portal_paymenttb WHERE trans_id={sql_lit(values['transId'])} AND rrr={sql_lit(values['rrr'])}")
        raise
    return {"ok": True, "operation": "payment_add", "amount": f"{amount:.2f}", "gateway_result": first,
            "verification": payment_check({"regno": regno, "session": session, "semester": semester, "transId": values["transId"]})}


def student_payments(payload: Mapping[str, Any]) -> dict[str, Any]:
    regno, session, semester = _payment_identity(payload)
    student_rows = _rows(sql(
        "SELECT regno,surname,first_name,other_name,prog_id,session_of_entry,status,portal_category_code "
        f"FROM studenttb WHERE regno={sql_lit(regno)} LIMIT 1"
    ), "Student payment lookup")
    if not student_rows:
        return {"student": None, "payments": [], "details": []}
    payments = _payment_rows(payload)
    details: list[dict[str, Any]] = []
    for row in payments:
        trans_id = row.get("trans_id")
        rrr = row.get("rrr")
        if trans_id is None or rrr is None:
            continue
        details.extend(_rows(sql(
            "SELECT * FROM portal_payment_detailtb "
            f"WHERE regno={sql_lit(regno)} AND session={sql_lit(session)} "
            f"AND trans_id={sql_lit(trans_id)} AND rrr={sql_lit(rrr)} "
            "ORDER BY id"
        ), "Student payment details"))
    return {"student": student_rows[0], "payments": payments, "details": details}


_PAYMENT_MAIN_EDITABLE = {
    "amount", "receipt_number", "payment_desc", "pay_item_id", "payment_date", "payment_time",
    "response_code", "response_desc", "session", "semester", "prog_id", "level", "rrr", "channel",
    "fee_code", "portal_category_code", "payment_status",
}
_PAYMENT_DETAIL_EDITABLE = {
    "folio_code", "amount", "session", "level", "prog_id", "stud_category", "studstatus",
    "portal_category_code", "payment_date", "payment_time", "trans_id", "rrr", "pay_item_id",
}


def _record_id(value: Any) -> int:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0 or int(text) > 2_147_483_647:
        raise DBQValidationError("payment record ID must be a positive integer")
    return int(text)


def _edit_values(payload: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    changes = payload.get("changes")
    if not isinstance(changes, Mapping) or not changes:
        raise DBQValidationError("changes must be a non-empty JSON object")
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        name = _column(key)
        if name not in allowed:
            raise DBQValidationError(f"Payment field cannot be edited: {name}")
        if name == "amount":
            value = f"{_checked_decimal(value, 'amount'):.2f}"
        clean[name] = value
    return clean


def _audit_payment_update(table: str, record_id: int, before: Mapping[str, Any], after: Mapping[str, Any], operator: str, verification: str) -> None:
    changed = {key: {"before": before.get(key), "after": after.get(key)} for key in set(before) | set(after) if before.get(key) != after.get(key)}
    audit = json.dumps({"verification_ref": verification, "changed": changed}, default=str, sort_keys=True)
    sql("INSERT INTO audit_logs_payment (table_name,record_id,action,changed_data,user,ip_address) VALUES ("
        f"{sql_lit(table)},{sql_lit(record_id)},'UPDATE',{sql_lit(audit)},{sql_lit(operator)},'admin-webui')")


def payment_update(payload: Mapping[str, Any]) -> dict[str, Any]:
    record_type = str(payload.get("recordType") or "main").strip().lower()
    if record_type not in {"main", "detail"}:
        raise DBQValidationError("recordType must be main or detail")
    record_id = _record_id(payload.get("recordId"))
    operator = str(payload.get("operator") or payload.get("modifier") or "").strip()
    verification = str(payload.get("verificationRef") or "").strip()
    if not operator or len(operator) > 64 or not verification or len(verification) > 256:
        raise DBQValidationError("operator and verification reference are required")
    allowed = _PAYMENT_MAIN_EDITABLE if record_type == "main" else _PAYMENT_DETAIL_EDITABLE
    changes = _edit_values(payload, allowed)
    if "amount" in changes and payload.get("confirmAmount") is not True:
        raise DBQValidationError("Changing a payment amount requires explicit confirmAmount=true")
    table = "portal_paymenttb" if record_type == "main" else "portal_payment_detailtb"
    before_rows = _rows(sql(f"SELECT * FROM {_quote_identifier(table)} WHERE id={record_id} LIMIT 1"), "Payment edit lookup")
    if len(before_rows) != 1:
        raise DBQError(f"Payment {record_type} record was not found uniquely")
    before = before_rows[0]
    if record_type == "main" and "amount" in changes:
        detail = _payment_detail_summary(before)
        if Decimal(str(changes["amount"])).quantize(Decimal("0.01")) != Decimal(str(detail.get("detail_total") or "0")).quantize(Decimal("0.01")):
            raise DBQValidationError("New main payment amount must equal its detail total")
    if record_type == "detail" and "amount" in changes:
        main = _rows(sql(
            "SELECT amount FROM portal_paymenttb "
            f"WHERE trans_id={sql_lit(before.get('trans_id'))} AND rrr={sql_lit(before.get('rrr'))} LIMIT 1"
        ), "Payment amount verification")
        detail_total = _payment_detail_summary(before)
        old_total = Decimal(str(detail_total.get("detail_total") or "0")).quantize(Decimal("0.01"))
        new_total = old_total - Decimal(str(before.get("amount") or "0")).quantize(Decimal("0.01")) + Decimal(str(changes["amount"])).quantize(Decimal("0.01"))
        main_total = Decimal(str(main[0].get("amount") or "0")).quantize(Decimal("0.01")) if main else Decimal("-1")
        if new_total != main_total:
            raise DBQValidationError("Edited detail amount must keep the detail total equal to the main payment")
    assignments = ",".join(f"{_quote_identifier(key)}={sql_lit(value)}" for key, value in changes.items())
    sql(f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE id={record_id} LIMIT 1")
    after_rows = _rows(sql(f"SELECT * FROM {_quote_identifier(table)} WHERE id={record_id} LIMIT 1"), "Payment edit verification")
    if len(after_rows) != 1:
        raise DBQError("Payment edit verification could not read the updated row")
    _audit_payment_update(table, record_id, before, after_rows[0], operator, verification)
    return {"ok": True, "operation": "payment_update", "record_type": record_type, "record_id": record_id, "before": before, "after": after_rows[0]}


def payment_reconcile(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = _payment_rows(payload)
    if len(rows) != 1:
        raise DBQError(f"Expected exactly one School Charges payment row, found {len(rows)}")
    row = rows[0]
    detail = _payment_detail_summary(row)
    try:
        if Decimal(str(row.get("amount") or 0)).quantize(Decimal("0.01")) != Decimal(str(detail.get("detail_total") or 0)).quantize(Decimal("0.01")):
            raise DBQValidationError("Refusing reconciliation because payment amounts differ")
    except DBQValidationError:
        raise
    except Exception as exc:
        raise DBQError("Payment amount could not be parsed") from exc
    if row.get("payment_status") != "Full" or row.get("response_code") != "01":
        raise DBQValidationError("Refusing reconciliation because payment is not Full/Approved")
    modifier = str(payload.get("modifier") or "").strip()
    verification = str(payload.get("verificationRef") or "").strip()
    if not modifier or not verification:
        raise DBQValidationError("modifier and verification reference are required")
    trans_id = str(row.get("trans_id"))
    sql(f"UPDATE portal_paymenttb SET fee_code={sql_lit(str(row.get('pay_item_id')))} WHERE id={int(row.get('id'))}")
    sql(f"UPDATE portal_payment_detailtb SET pay_item_id={sql_lit(str(row.get('pay_item_id')))} WHERE regno={sql_lit(row.get('regno'))} AND session={sql_lit(row.get('session'))} AND trans_id={sql_lit(trans_id)} AND rrr={sql_lit(row.get('rrr'))}")
    audit = json.dumps({"verification_ref": verification, "old_fee_code": row.get("fee_code"), "new_fee_code": row.get("pay_item_id")}, sort_keys=True)
    sql("INSERT INTO audit_logs_payment (table_name,record_id,action,changed_data,user,ip_address) VALUES ("
        f"'portal_paymenttb',{sql_lit(trans_id)},'UPDATE',{sql_lit(audit)},{sql_lit(modifier)},'admin-webui')")
    return {"ok": True, "operation": "payment_reconcile", "verification": payment_check(payload)}


def execute_action(payload: Mapping[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation") or "").strip()
    if operation == "insert":
        return generic_insert(str(payload.get("table") or ""), payload.get("values") if isinstance(payload.get("values"), Mapping) else {})
    if operation == "update":
        return generic_update(str(payload.get("table") or ""), payload.get("keys") if isinstance(payload.get("keys"), Mapping) else {}, payload.get("values") if isinstance(payload.get("values"), Mapping) else {})
    if operation == "delete":
        return generic_delete(str(payload.get("table") or ""), payload.get("keys") if isinstance(payload.get("keys"), Mapping) else {}, payload.get("confirmed"))
    if operation == "student_account_lookup":
        return student_account_lookup(payload)
    if operation == "student_password_format_diagnostics":
        return student_password_format_diagnostics(payload)
    if operation == "student_password_reset":
        return student_password_reset(payload)
    if operation == "score_update":
        return score_update(payload)
    if operation == "score_publish":
        return score_publish(payload)
    if operation == "score_add":
        return score_add(payload)
    if operation == "batch_check":
        return batch_check(payload)
    if operation == "map_check":
        return map_check(payload)
    if operation == "payment_check":
        return payment_check(payload)
    if operation == "eligibility_check":
        return payment_eligibility(payload)
    if operation == "payment_add":
        return payment_add(payload)
    if operation == "payment_reconcile":
        return payment_reconcile(payload)
    if operation == "payment_update":
        return payment_update(payload)
    if operation == "dbq_ping":
        return ping()
    if operation == "table_search":
        return table_search(str(payload.get("table") or ""), str(payload.get("term") or ""), payload.get("limit", 50))
    if operation == "person_search":
        return person_search(str(payload.get("term") or ""), str(payload.get("scope") or "all"), payload.get("limit", 50))
    raise DBQValidationError("Unknown database operation")
