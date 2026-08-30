from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.agent.tools.uniabuja_admin import _access_status, _verified_admin_context
from nanobot.agent.tools.uniabuja_student import (
    _request as _student_supabase_request,
)
from nanobot.agent.tools.uniabuja_student import (
    _status,
    _student_identity,
    _telegram_numeric_sender_id,
)

_DEFAULT_URL = "https://transcript.uniabuja.edu.ng/upload_files/dbq.php"
_MAX_RESULT_CHARS = 16_000
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_COURSE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SEMESTERS = {"1st", "2nd", "3rd", "4th", "-"}
_SAFE_SCHEMA_TABLES = {
    "studenttb",
    "regtb",
    "coursetb",
    "deptcoursetb",
    "course_allocationtb",
    "files_uploadtb",
    "result_upload_datetb",
    "settings_gradtb",
    "chargestb",
    "charges_itemtb",
    "charges_typetb",
    "portal_general_payment_setup",
    "settings_paymenttb",
    "portal_paymenttb",
    "portal_payment_detailtb",
    "course_evaluationtb",
}


class UniAbujaTranscriptError(RuntimeError):
    pass


def _url() -> str:
    return (os.getenv("UNIABUJA_DBQ_URL") or os.getenv("UNIABUJA_TRANSCRIPT_URL") or _DEFAULT_URL).strip()


def _key() -> str:
    return (os.getenv("UNIABUJA_DBQ_KEY") or os.getenv("UNIABUJA_TRANSCRIPT_KEY") or "").strip()


def _configured() -> bool:
    return bool(_url() and _key())


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _checked(value: Any, name: str, pattern: re.Pattern[str] = _TOKEN_RE) -> str:
    text = str(value or "").strip()
    if not text or not pattern.fullmatch(text):
        raise UniAbujaTranscriptError(f"Invalid {name}")
    return text


def _regno(value: Any) -> str:
    return _checked(value, "registration number")


def _course(value: Any) -> str:
    return _checked(str(value).upper(), "course code", _COURSE_RE)


def _session(value: Any) -> str:
    return _checked(value, "session")


def _semester(value: Any) -> str:
    text = _checked(value, "semester")
    if text not in _SEMESTERS:
        raise UniAbujaTranscriptError("Semester is invalid")
    return text


def _operator(value: Any, name: str = "operator") -> str:
    return _checked(value, name)


def _sql(sql: str) -> Any:
    if not _configured():
        raise UniAbujaTranscriptError("UniAbuja transcript gateway is not configured on the server")
    try:
        with httpx.Client(timeout=35.0, follow_redirects=False) as client:
            response = client.post(
                _url(),
                data={"sql": sql},
                headers={"X-DBQ-Key": _key(), "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise UniAbujaTranscriptError("UniAbuja transcript gateway is unreachable") from exc
    if response.status_code != 200:
        raise UniAbujaTranscriptError(f"UniAbuja transcript gateway returned HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise UniAbujaTranscriptError("UniAbuja transcript gateway returned invalid JSON") from exc
    if isinstance(result, dict) and result.get("error"):
        raise UniAbujaTranscriptError("UniAbuja transcript gateway rejected the query")
    return result


def _safe_output(value: Any) -> str:
    text = str(value)
    text = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|private[_-]?key|credential)\b\s*[:=]\s*[^,\s}]+",
        "[redacted]",
        text,
    )
    return text[-_MAX_RESULT_CHARS:] or "(no rows)"


def _student_sql(regno: str, session: str | None) -> str:
    conditions = [f"LOWER(regno)=LOWER({_literal(regno)})"]
    if session:
        conditions.append(f"session={_literal(_session(session))}")
    where = " AND ".join(conditions)
    return (
        "SELECT regno,surname,first_name,other_name,prog_id,session_of_entry,status,portal_category_code "
        f"FROM studenttb WHERE LOWER(regno)=LOWER({_literal(regno)}) LIMIT 1; "
        "SELECT regno,course_code,title,unit,semester,status,"
        "score_decode(final_ca) AS decoded_ca,score_decode(final_exam) AS decoded_exam,"
        "upload_status,ca_entry_by,exam_entry_by,reg_entry_by,modify_by,regstatus,session "
        f"FROM regtb WHERE {where} ORDER BY session DESC,semester,course_code"
    )


def _score_check_sql(regno: str, course: str, session: str, semester: str) -> str:
    return (
        "SELECT regno,course_code,title,unit,status,semester,fact_course,dept_id,prog_id,level,"
        "session,regstatus,ca,exam,score_decode(final_ca) AS decoded_ca,"
        "score_decode(final_exam) AS decoded_exam,final_ca,final_exam,practical,upload_status,"
        "ca_entry_by,exam_entry_by,reg_entry_by,added_date,added_time,"
        "portal_category_code,modify_by,data_upload_status FROM regtb WHERE "
        f"LOWER(regno)=LOWER({_literal(regno)}) AND course_code={_literal(course)} "
        f"AND session={_literal(session)} AND semester={_literal(semester)}"
    )


def _student_auth_email(ctx: Any) -> str:
    rows = _student_supabase_request(
        "GET",
        "/rest/v1/telegram_accounts",
        params={
            "select": "auth_email",
            "telegram_user_id": f"eq.{_telegram_numeric_sender_id(ctx)}",
            "limit": "1",
        },
    )
    email = str(rows[0].get("auth_email") or "").strip() if isinstance(rows, list) and rows else ""
    if not email or "@" not in email or len(email) > 320:
        raise UniAbujaTranscriptError("The authenticated student has no usable portal email mapping")
    return email


def _student_lookup_sql(email: str) -> str:
    email_lit = _literal(email)
    return (
        "SELECT regno,surname,first_name,other_name,prog_id,session_of_entry,status,portal_category_code "
        f"FROM studenttb WHERE LOWER(email)=LOWER({email_lit}) LIMIT 1; "
        "SELECT regno,course_code,title,unit,semester,status,"
        "score_decode(final_ca) AS decoded_ca,score_decode(final_exam) AS decoded_exam,"
        "upload_status,ca_entry_by,exam_entry_by,reg_entry_by,modify_by,regstatus,session "
        f"FROM regtb WHERE LOWER(regno) IN (SELECT LOWER(regno) FROM studenttb WHERE LOWER(email)=LOWER({email_lit})) "
        "ORDER BY session DESC,semester,course_code"
    )


def _student_score_check_sql(email: str, course: str, session: str, semester: str) -> str:
    email_lit = _literal(email)
    return (
        "SELECT regno,course_code,title,unit,status,semester,fact_course,dept_id,prog_id,level,"
        "session,regstatus,score_decode(final_ca) AS decoded_ca,score_decode(final_exam) AS decoded_exam,"
        "upload_status,ca_entry_by,exam_entry_by,reg_entry_by,modify_by,portal_category_code "
        f"FROM regtb WHERE LOWER(regno) IN (SELECT LOWER(regno) FROM studenttb WHERE LOWER(email)=LOWER({email_lit})) "
        f"AND course_code={_literal(course)} AND session={_literal(session)} AND semester={_literal(semester)}"
    )


def _student_payment_check_sql(email: str, session: str, semester: str) -> str:
    email_lit = _literal(email)
    return (
        "SELECT id,regno,amount,receipt_number,payment_desc,pay_item_id,payment_date,payment_time,"
        "response_code,response_desc,session,semester,prog_id,level,trans_id,entry_by,rrr,channel,"
        "fee_code,portal_category_code,payment_status FROM portal_paymenttb WHERE LOWER(regno) IN "
        f"(SELECT LOWER(regno) FROM studenttb WHERE LOWER(email)=LOWER({email_lit})) "
        f"AND session={_literal(session)} AND semester={_literal(semester)} AND payment_desc='School Charges' "
        "ORDER BY payment_date,payment_time,id"
    )


def _student_eligibility_sql(email: str, session: str, semester: str) -> str:
    email_lit = _literal(email)
    return (
        "SELECT regno,amount,receipt_number,pay_item_id,fee_code,trans_id,rrr,payment_status,"
        "response_code,session,semester FROM portal_paymenttb WHERE LOWER(regno) IN "
        f"(SELECT LOWER(regno) FROM studenttb WHERE LOWER(email)=LOWER({email_lit})) "
        f"AND session={_literal(session)} AND semester={_literal(semester)} AND payment_desc='School Charges' "
        "ORDER BY payment_date,payment_time,id"
    )


def _payment_check_sql(regno: str, session: str, semester: str) -> str:
    return (
        "SELECT id,regno,amount,receipt_number,payment_desc,pay_item_id,payment_date,payment_time,"
        "response_code,response_desc,session,semester,prog_id,level,trans_id,entry_by,rrr,channel,"
        "fee_code,portal_category_code,payment_status FROM portal_paymenttb WHERE "
        f"LOWER(regno)=LOWER({_literal(regno)}) AND session={_literal(session)} "
        f"AND semester={_literal(semester)} AND payment_desc='School Charges' "
        "ORDER BY payment_date,payment_time,id"
    )


def _eligibility_sql(regno: str, session: str, semester: str) -> str:
    return (
        "SELECT regno,amount,receipt_number,pay_item_id,fee_code,trans_id,rrr,payment_status,"
        "response_code,session,semester FROM portal_paymenttb WHERE "
        f"LOWER(regno)=LOWER({_literal(regno)}) AND session={_literal(session)} "
        f"AND semester={_literal(semester)} AND payment_desc='School Charges' "
        "ORDER BY payment_date,payment_time,id"
    )


def _schema_sql(table: str) -> str:
    if table not in _SAFE_SCHEMA_TABLES:
        raise UniAbujaTranscriptError("That table is not in the safe schema allowlist")
    return f"DESCRIBE {table}"


def _score_update_sql(regno: str, course: str, session: str, semester: str, ca: int, exam: int, operator: str, publish: bool) -> str:
    if not 0 <= ca <= 100 or not 0 <= exam <= 100:
        raise UniAbujaTranscriptError("CA and exam must each be between 0 and 100")
    publication = ", upload_status='Yes'" if publish else ""
    return (
        "UPDATE regtb SET final_ca=score_encode(" + str(ca) + "), final_exam=score_encode(" + str(exam) + "), "
        "ca=NULL, exam=NULL, ca_entry_by=" + _literal(operator) + ", exam_entry_by=" + _literal(operator) + ", "
        "modify_by=" + _literal(operator) + ", data_upload_status=NULL" + publication + " WHERE "
        f"LOWER(regno)=LOWER({_literal(regno)}) AND course_code={_literal(course)} "
        f"AND session={_literal(session)} AND semester={_literal(semester)}"
    )


def _publish_sql(regno: str, course: str, session: str, semester: str, operator: str) -> str:
    return (
        "UPDATE regtb SET upload_status='Yes', data_upload_status=NULL, modify_by=" + _literal(operator) + " WHERE "
        f"LOWER(regno)=LOWER({_literal(regno)}) AND course_code={_literal(course)} "
        f"AND session={_literal(session)} AND semester={_literal(semester)}"
    )


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "Safe UniAbuja transcript-gateway operation",
            enum=["student_lookup", "score_check", "payment_check", "eligibility_check", "schema", "score_update", "publish_row"],
        ),
        regno=StringSchema("Registration number, for example 22/205EEE/172"),
        course=StringSchema("Course code"),
        session=StringSchema("Academic session"),
        semester=StringSchema("Semester"),
        table=StringSchema("Allowlisted table name for schema inspection"),
        ca=StringSchema("CA score from 0 to 100"),
        exam=StringSchema("Exam score from 0 to 100"),
        operator=StringSchema("Verified operator identifier"),
        publish=StringSchema("Whether the score row should be published", enum=["true", "false"]),
        confirm=StringSchema("Must be true to execute a write; otherwise returns a dry-run proposal", enum=["true", "false"]),
    )
)
class UniAbujaTranscriptTool(Tool):
    """Controlled adapter for the UniAbuja transcript DBQ gateway in the supplied TXT."""

    _scopes = {"core"}
    config_key = "uniabuja_transcript"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _configured()

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "uniabuja_transcript"

    @property
    def description(self) -> str:
        return (
            "Use the UniAbuja transcript DBQ gateway described in the supplied database TXT. "
            "Administrator-only actions can look up a student by registration number, inspect a score/payment, "
            "check eligibility, inspect an allowlisted table schema, or propose/execute a score update or publication. "
            "Writes require the UniAbuja database write switch and explicit confirm=true; without confirmation they are dry runs. "
            "Ordinary authenticated students may use only safe read operations when the administrator’s read switch is enabled, "
            "and arbitrary student-record lookups are not exposed to them. Never execute arbitrary SQL, payment writes, shell, or credential operations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["student_lookup", "score_check", "payment_check", "eligibility_check", "schema", "score_update", "publish_row"]},
                "regno": {"type": "string"},
                "course": {"type": "string"},
                "session": {"type": "string"},
                "semester": {"type": "string"},
                "table": {"type": "string"},
                "ca": {"type": "string"},
                "exam": {"type": "string"},
                "operator": {"type": "string"},
                "publish": {"type": "string", "enum": ["true", "false"]},
                "confirm": {"type": "string", "enum": ["true", "false"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @staticmethod
    def _true(value: Any) -> bool:
        return str(value or "").strip().lower() == "true"

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        try:
            action = str(kwargs.get("action") or "").strip().lower()
            admin_ctx = None
            admin_id = ""
            if action in {"student_lookup", "score_check", "payment_check", "eligibility_check", "schema", "score_update", "publish_row"}:
                # The verified-admin path is checked first. Student access is only
                # allowed for the explicitly safe read actions below.
                try:
                    admin_ctx, admin_id = await asyncio.to_thread(_verified_admin_context)
                except Exception:
                    admin_ctx = None
            if admin_ctx is None:
                if action not in {"student_lookup", "score_check", "payment_check", "eligibility_check"}:
                    raise UniAbujaTranscriptError("This transcript operation requires the verified administrator")
                student_ctx, _ = await asyncio.to_thread(_student_identity)
                status = await asyncio.to_thread(_status)
                if not status["read_access"]:
                    raise UniAbujaTranscriptError("UniAbuja student read access is disabled by the administrator")
                email = await asyncio.to_thread(_student_auth_email, student_ctx)
                if action == "student_lookup":
                    result = await asyncio.to_thread(_sql, _student_lookup_sql(email))
                elif action == "score_check":
                    result = await asyncio.to_thread(
                        _sql,
                        _student_score_check_sql(
                            email,
                            _course(kwargs.get("course")),
                            _session(kwargs.get("session")),
                            _semester(kwargs.get("semester")),
                        ),
                    )
                elif action == "payment_check":
                    result = await asyncio.to_thread(
                        _sql,
                        _student_payment_check_sql(
                            email,
                            _session(kwargs.get("session")),
                            _semester(kwargs.get("semester")),
                        ),
                    )
                else:
                    result = await asyncio.to_thread(
                        _sql,
                        _student_eligibility_sql(
                            email,
                            _session(kwargs.get("session")),
                            _semester(kwargs.get("semester")),
                        ),
                    )
                return _safe_output(result)
            if action == "student_lookup":
                result = await asyncio.to_thread(_sql, _student_sql(_regno(kwargs.get("regno")), kwargs.get("session")))
                return _safe_output(result)
            if action == "score_check":
                result = await asyncio.to_thread(
                    _sql,
                    _score_check_sql(
                        _regno(kwargs.get("regno")),
                        _course(kwargs.get("course")),
                        _session(kwargs.get("session")),
                        _semester(kwargs.get("semester")),
                    ),
                )
                return _safe_output(result)
            if action == "payment_check":
                result = await asyncio.to_thread(
                    _sql,
                    _payment_check_sql(_regno(kwargs.get("regno")), _session(kwargs.get("session")), _semester(kwargs.get("semester"))),
                )
                return _safe_output(result)
            if action == "eligibility_check":
                result = await asyncio.to_thread(
                    _sql,
                    _eligibility_sql(_regno(kwargs.get("regno")), _session(kwargs.get("session")), _semester(kwargs.get("semester"))),
                )
                return _safe_output(result)
            if action == "schema":
                result = await asyncio.to_thread(_sql, _schema_sql(_checked(kwargs.get("table"), "table name")))
                return _safe_output(result)
            status = await asyncio.to_thread(_access_status)
            if not status["write_access"]:
                raise UniAbujaTranscriptError("UniAbuja database write access is disabled by the administrator")
            operator = _operator(kwargs.get("operator") or admin_id, "operator")
            confirm = self._true(kwargs.get("confirm"))
            publish = self._true(kwargs.get("publish"))
            if action == "score_update":
                try:
                    ca = int(str(kwargs.get("ca") or ""))
                    exam = int(str(kwargs.get("exam") or ""))
                except ValueError:
                    raise UniAbujaTranscriptError("CA and exam must be integers") from None
                query = _score_update_sql(
                    _regno(kwargs.get("regno")),
                    _course(kwargs.get("course")),
                    _session(kwargs.get("session")),
                    _semester(kwargs.get("semester")),
                    ca,
                    exam,
                    operator,
                    publish,
                )
            elif action == "publish_row":
                query = _publish_sql(
                    _regno(kwargs.get("regno")),
                    _course(kwargs.get("course")),
                    _session(kwargs.get("session")),
                    _semester(kwargs.get("semester")),
                    operator,
                )
            else:
                raise UniAbujaTranscriptError("Unknown transcript operation")
            if not confirm:
                return f"DRY RUN only. Proposed UniAbuja transcript write:\n{query}"
            result = await asyncio.to_thread(_sql, query)
            logger.warning("Verified UniAbuja administrator executed a transcript write action")
            return _safe_output(result)
        except UniAbujaTranscriptError as exc:
            return ToolResult.error(f"UniAbuja transcript access denied: {exc}")
        except Exception as exc:
            logger.exception("UniAbuja transcript operation failed")
            return ToolResult.error(f"UniAbuja transcript error: {type(exc).__name__}")
