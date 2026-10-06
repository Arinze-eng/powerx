"""Agent tool: decide whether a Nigerian payment receipt is internally consistent.

Use when someone shows a receipt, transfer slip, POS proof or fee invoice and the
question is whether the money actually moved. Catches the fraud that is actually
common: a reference whose hidden timestamp contradicts the printed date, an
invoice presented as a paid receipt, and arithmetic that does not add up.

Two things this tool will never claim:
  * VERIFIED from local analysis alone. Self-consistency is not proof of payment.
  * A verdict about a person. It returns violated rules and evidence; a human
    decides.

Set REMITA_GUEST_LOOKUP_ENABLED=1 only with the payment institution's agreement
before using the ledger mode; see nanobot/forensics/remita_ledger.py for why the
lookup is single-reference, rate-limited and audit-logged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from nanobot.forensics import receipt_rules as rules
from nanobot.forensics.remita_ledger import RemitaLedger

from ..tools.base import Tool, ToolResult, tool_parameters

_RAILS = ("opay", "kuda", "access", "gtbank", "remita", "moniepoint", "auto")

# Moniepoint tails are Snowflake IDs: the creation millisecond is recoverable by
# right-shifting 22 bits off a known epoch, which is why an "opaque" alphanumeric
# reference still carries a verifiable timestamp.
_SNOWFLAKE_EPOCH_MS = 1_288_834_974_657


@tool_parameters({
    "type": "object",
    "properties": {
        "rail": {
            "type": "string",
            "enum": list(_RAILS),
            "description": "Payment rail. 'auto' infers it from the reference shape.",
        },
        "reference": {
            "type": "string",
            "description": "Transaction reference / number / RRR, copied exactly.",
        },
        "session_id": {
            "type": "string",
            "description": "OPay Session ID when the receipt prints one - a second "
                           "hidden clock to cross-check the reference against.",
        },
        "printed_date": {
            "type": "string",
            "description": "Date as the receipt prints it, e.g. '2026-09-23', "
                           "'08 MAR 2022', 'Sep 23, 2026'.",
        },
        "printed_time": {
            "type": "string",
            "description": "Time as printed, e.g. '01:00 PM', '02:26:35'. Omit if "
                           "the receipt prints no time; the clock rules then report "
                           "not_applicable instead of guessing.",
        },
        "sender_account": {
            "type": "string",
            "description": "Payer account number, for Kuda reference-tail checks.",
        },
        "amount": {"type": "number", "description": "Principal as printed."},
        "charges": {"type": "number", "description": "Service charge as printed."},
        "vat": {"type": "number", "description": "VAT as printed."},
        "total": {"type": "number", "description": "Total as printed."},
        "amount_in_words": {
            "type": "string",
            "description": "The amount spelled out on the document.",
        },
        "claimed_identity": {
            "type": "string",
            "description": "Name or matric/ID the presenter claims. Cross-checked "
                           "against the document's payer fields.",
        },
        "mode": {
            "type": "string",
            "enum": ["local", "ledger"],
            "description": "'local' runs offline consistency rules (default, always "
                           "safe). 'ledger' also asks Remita whether the reference "
                           "was actually paid - requires configured consent.",
        },
        "presented_by_claimant": {
            "type": "boolean",
            "description": "True only when the person being checked handed over "
                           "this reference. Guards against probing references that "
                           "nobody presented.",
        },
    },
    "required": ["reference"],
})
class ReceiptAuthenticityTool(Tool):
    """Cross-check a payment reference against the document that carries it."""

    config_key: ClassVar[str] = "receipt_authenticity"
    _scopes: ClassVar[set[str]] = {"core", "subagent"}

    def __init__(self, ledger: RemitaLedger | None = None):
        self._ledger = ledger

    @property
    def ledger(self) -> RemitaLedger:
        if self._ledger is None:
            self._ledger = RemitaLedger()
        return self._ledger

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        cfg = getattr(ctx.config, "receipt_authenticity", None)
        ledger = None
        if cfg is not None:
            ledger = RemitaLedger(
                enabled=bool(getattr(cfg, "remita_lookup_enabled", False)),
                min_interval=float(getattr(cfg, "remita_lookup_min_interval", 2.0)),
            )
        return cls(ledger=ledger)

    @property
    def name(self) -> str:
        return "receipt_authenticity"

    @property
    def description(self) -> str:
        return (
            "Check a Nigerian payment receipt (OPay, Kuda, Access, GTBank, "
            "Moniepoint, Remita) for internal contradictions that indicate "
            "fabrication, and optionally confirm the reference against the "
            "payment rail. Returns violated rules with evidence, never a verdict "
            "about a person. Local mode proves a receipt WRONG; it cannot prove a "
            "payment HAPPENED, so a clean local result is reported as UNVERIFIED, "
            "not genuine. Use mode='ledger' when the presenter handed you the "
            "reference and the institution has agreed to lookups."
        )

    async def execute(self, **kwargs: Any) -> Any:
        reference = str(kwargs.get("reference") or "").strip()
        if not reference:
            return ToolResult.error("Provide the 'reference' printed on the receipt.")

        rail = str(kwargs.get("rail") or "auto").strip().lower()
        if rail == "auto":
            rail = self._guess_rail(reference, bool(str(kwargs.get("session_id") or "")))

        assessment = self._assess(rail, kwargs)
        extra = self._arithmetic(kwargs) + self._identity(kwargs)
        assessment.results.extend(extra)

        payload: dict[str, Any] = {
            "rail": rail,
            "reference": reference,
            **assessment.to_dict(),
        }

        mode = str(kwargs.get("mode") or "local").strip().lower()
        if mode == "ledger" and rail == "remita":
            outcome = self.ledger.verify(
                reference,
                presented_by_claimant=bool(kwargs.get("presented_by_claimant")),
            )
            payload["ledger"] = outcome.to_dict()
            if outcome.state == "NOT_PAID":
                payload["state"] = "INVALID"
                payload["violations"].append({
                    "rule": "REMITA-NOT-PAID", "detail": outcome.detail})
            elif outcome.state == "VERIFIED":
                payload["state"] = "VERIFIED"
        elif mode == "ledger":
            payload["ledger"] = {
                "state": "UNAVAILABLE",
                "detail": "Ledger confirmation is wired for Remita references only; "
                          "other rails need their own payee-side integration.",
            }

        payload["guidance"] = self._guidance(payload["state"], rail)
        return ToolResult(json.dumps(payload, indent=2, default=str))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _guess_rail(reference: str, has_session: bool) -> str:
        d = rules._digits(reference)
        alpha = "".join(c for c in reference if not c.isdigit())
        if alpha.upper().startswith("NXG"):
            return "access"
        if has_session or (len(d) == 24 and d.isdigit()):
            return "opay"
        if d and d[0:6] == "090267" and len(d) == 30:
            return "kuda"
        if "MPT" in alpha.upper():
            return "moniepoint"
        if len(d) == 12 and not alpha:
            return "remita"
        if len(d) == 30:
            return "gtbank"
        return "unknown"

    def _assess(self, rail: str, kw: dict) -> rules.ReceiptAssessment:
        ref = str(kw.get("reference") or "")
        pd = str(kw.get("printed_date") or "")
        pt = str(kw.get("printed_time") or "")
        if rail == "opay":
            return rules.assess_opay(
                ref, str(kw.get("session_id") or ""), pd, pt)
        if rail == "kuda":
            return rules.assess_kuda(
                ref, pd, pt, str(kw.get("sender_account") or ""))
        if rail in ("access", "gtbank"):
            return rules.assess_nip(rail, ref, pd, pt)
        if rail == "remita":
            return rules.assess_remita(ref, pd)
        if rail == "moniepoint":
            return self._assess_moniepoint(ref, pd, pt)
        a = rules.ReceiptAssessment(rail=rail)
        a.results.append(rules.RuleResult(
            "RAIL-UNKNOWN", rules.NA,
            f"Cannot identify the rail from '{ref}'. Tell me which app issued the "
            "receipt: format rules are per-rail and a wrong guess false-positives."))
        return a

    @staticmethod
    def _assess_moniepoint(ref: str, pd: str, pt: str) -> rules.ReceiptAssessment:
        """Moniepoint tails are Snowflake IDs: (id >> 22) + epoch = creation ms.

        That is why an 'opaque' alphanumeric reference still carries a provable
        timestamp - three unrelated samples all decoded to their printed minute.
        """
        a = rules.ReceiptAssessment(rail="moniepoint")
        printed, has_time = rules._as_datetime(pd, pt)
        big = re.findall(r"\d{17,20}", ref)
        if not big:
            a.results.append(rules.RuleResult(
                "MP-SNOWFLAKE", rules.NA,
                "no 19-digit tail found, so no creation timestamp can be decoded"))
            return a
        ms = (int(big[0]) >> 22) + _SNOWFLAKE_EPOCH_MS
        try:
            wat = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(rules.WAT)
        except (OverflowError, OSError, ValueError):
            a.results.append(rules.RuleResult(
                "MP-SNOWFLAKE", rules.NA, "tail did not decode as a timestamp"))
            return a
        if printed is None or not has_time:
            a.results.append(rules.RuleResult(
                "MP-SNOWFLAKE", rules.NA,
                f"tail decodes to {wat:%Y-%m-%d %H:%M:%S} WAT; "
                + ("no printed date to compare against" if printed is None
                   else "receipt prints no time, so nothing to compare")))
            return a
        wat_naive = wat.replace(tzinfo=None)
        same_day = wat_naive.strftime("%Y-%m-%d") == printed.strftime("%Y-%m-%d")
        same = printed.replace(year=wat_naive.year, month=wat_naive.month,
                               day=wat_naive.day)
        close = abs((wat_naive - same).total_seconds()) <= 120
        status = rules.PASS if (same_day and close) else rules.FAIL
        a.results.append(rules.RuleResult(
            "MP-SNOWFLAKE", status,
            f"reference embeds {wat:%Y-%m-%d %H:%M:%S} WAT; receipt prints "
            f"{printed:%Y-%m-%d %H:%M:%S}"))
        return a

    @staticmethod
    def _arithmetic(kw: dict) -> list[rules.RuleResult]:
        out: list[rules.RuleResult] = []
        amount, charges = kw.get("amount"), kw.get("charges")
        vat, total = kw.get("vat"), kw.get("total")
        if all(v is not None for v in (amount, charges, vat, total)):
            expected_vat = round(float(charges) * 0.075, 2)
            out.append(rules.RuleResult(
                "ARITH-VAT",
                rules.PASS if abs(expected_vat - float(vat)) < 0.005 else rules.FAIL,
                f"7.5% of the {float(charges):,.2f} charge is {expected_vat:,.2f}; "
                f"the document prints {float(vat):,.2f}. VAT applies to the charge "
                "only, never the principal."))
            expected_total = round(float(amount) + float(charges) + expected_vat, 2)
            out.append(rules.RuleResult(
                "ARITH-TOTAL",
                rules.PASS if abs(expected_total - float(total)) < 0.005 else rules.FAIL,
                f"principal+charge+VAT = {expected_total:,.2f}; the document prints "
                f"{float(total):,.2f}"))
        words = str(kw.get("amount_in_words") or "").strip()
        if words and total is not None:
            out.append(rules.RuleResult(
                "ARITH-WORDS", rules.NA,
                "words present; automated comparison needs the amount-in-words "
                f"generator - check by eye that it describes {float(total):,.2f}"))
        return out

    @staticmethod
    def _identity(kw: dict) -> list[rules.RuleResult]:
        claimed = str(kw.get("claimed_identity") or "").strip()
        ref = str(kw.get("reference") or "")
        if not claimed:
            return []
        digits = rules._digits(claimed)
        if digits and digits in rules._digits(ref):
            return [rules.RuleResult("ID-IN-REFERENCE", rules.PASS,
                                     "claimed identifier appears in the reference")]
        return [rules.RuleResult(
            "ID-UNCHECKED", rules.NA,
            f"claimed identity '{claimed}' is not embedded in the reference; "
            "compare it against the payer name/email the ledger returns")]

    @staticmethod
    def _guidance(state: str, rail: str) -> str:
        if state == "INVALID":
            return (
                "The document contradicts itself. Refer it to a human with the "
                "violated rules attached; do not announce fraud to the presenter.")
        if state == "VERIFIED":
            return "Settled according to the rail itself. Highest confidence."
        return (
            f"Nothing contradicts itself, which is NOT proof of payment. Recycled "
            f"references (a real {rail} payment shown for a different amount or "
            "beneficiary) and unpaid invoices pass every local rule. Confirm "
            "against the payee's own ledger before accepting it.")
