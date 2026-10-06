"""Remita reference status lookup, with the restraint the data requires.

Remita's payment page resolves a Remita Retrieval Reference (RRR) to its live
state - biller, amount, and whether the money has actually arrived. That is the
one signal the offline rules in ``receipt_rules`` structurally cannot produce,
and it is what turns "the numbers are self-consistent" into "the payment
happened".

Why this module is deliberately small and awkward to misuse
-----------------------------------------------------------
Each response carries a named person's identity, contact address and financial
position. An RRR is also not the unguessable 12-digit value it appears to be: it
is a short institution prefix followed by a counter, so neighbouring references
resolve to neighbouring payers. That makes *capability* the risk, not intent -
any tool that can check one can check a thousand, and the thousand-person
version is a data breach plus an offence under the Cybercrimes (Prohibition,
Prevention) Act 2015 and the Nigeria Data Protection Act 2023.

So the enforcement here is structural, not a comment asking people to be good:

* **No batch, iterate, range or sweep helper exists.** The function takes one
  reference, and there is nothing to grep for when someone wants a loop.
* A reference is refused unless the caller asserts it was **presented to the
  verifying party** by the claimant. Probing to discover whether a reference
  exists is enumeration, and is answered with a refusal, not a lookup.
* A minimum interval between calls, so incidental loops cannot turn into a scan.
* Successful lookups are written to a local audit log; refusals and errors are
  not, so the log is an accurate record of people whose data was read.
* **Opt-in.** It stays inert until an operator configures it. If a school can
  supply its own contractor credentials, the documented server-to-server API is
  the correct integration and this unofficial path should not be used at all -
  it is undocumented, can gain auth or move without notice, and a tool that
  silently starts answering "pending" for everyone is worse than no tool.

Return values distinguish NOT_FOUND from UNAVAILABLE on purpose. "This reference
did not resolve" is often an expired invoice or a wrong rail, not a fraud, and
collapsing those would have the agent accuse innocent people.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ENDPOINT = os.environ.get(
    "REMITA_GUEST_LOOKUP_URL",
    "https://api-remitacenta-login.remita.net/services/guestbridge-service"
    "/api/v1/biller/lookup/{rrr}",
)
ENABLED_ENV = "NANOBOT_REMITA_LOOKUP_ENABLED"
MIN_INTERVAL_ENV = "NANOBOT_REMITA_LOOKUP_MIN_INTERVAL"
AUDIT_ENV = "NANOBOT_REMITA_LOOKUP_AUDIT"
DEFAULT_AUDIT = Path.home() / ".nanobot" / "remita_lookup_audit.jsonl"

#: Remita's own success code for a resolved lookup.
_OK = "00"
_PAID = {"SUCCESS", "SUCCESSFUL", "PAID", "CONFIRMED", "COMPLETED", "DEBITED"}
_UNPAID = {"PENDING", "UNPAID", "NOT_PAID", "INITIATED", "GENERATED", "CREATED"}


@dataclass(frozen=True)
class LookupOutcome:
    state: str          # VERIFIED | NOT_PAID | NOT_FOUND | UNAVAILABLE | REFUSED | ERROR
    detail: str
    fields: dict | None = None

    def to_dict(self) -> dict:
        out = {"state": self.state, "detail": self.detail}
        if self.fields:
            out["fields"] = self.fields
        return out


def normalise_rrr(raw: str) -> str:
    """Return the digit body of an RRR, or '' when it is not shaped like one."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) == 12 else ""


class RemitaLedger:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, enabled: bool | None = None,
                 min_interval: float | None = None, audit_path: Path | None = None):
        self.endpoint = endpoint
        if enabled is None:
            enabled = os.environ.get(ENABLED_ENV, "").strip().lower() in {
                "1", "true", "yes", "on"}
        self.enabled = enabled
        if min_interval is None:
            try:
                min_interval = float(os.environ.get(MIN_INTERVAL_ENV, "2.0"))
            except ValueError:
                min_interval = 2.0
        self.min_interval = max(min_interval, 0.5)
        self.audit_path = audit_path or Path(
            os.environ.get(AUDIT_ENV) or DEFAULT_AUDIT)
        self._last = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.endpoint)

    def verify(self, rrr: str, *, presented_by_claimant: bool) -> LookupOutcome:
        """Resolve one reference that a claimant handed to the verifying party."""
        if not presented_by_claimant:
            return LookupOutcome(
                "REFUSED",
                "Refused: this reference was not presented by a claimant to a "
                "verifying party. Querying references to discover whether they "
                "exist is enumeration of other people's payment records - it is "
                "not verification, and it is unlawful. Only look up the reference "
                "someone actually handed over.")
        if not self.configured:
            return LookupOutcome(
                "UNAVAILABLE",
                "Remita lookup is disabled. Set " + ENABLED_ENV + "=1 only after "
                "agreeing terms with the institution whose payments are checked. "
                "Prefer Remita's documented contractor API: this endpoint is "
                "unofficial and may stop working at any time. Until then report "
                "UNVERIFIED - never VERIFIED.")
        digits = normalise_rrr(rrr)
        if not digits:
            return LookupOutcome("ERROR", f"'{rrr}' is not a 12-digit RRR")

        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

        try:
            req = urllib.request.Request(
                self.endpoint.format(rrr=digits),
                headers={"Accept": "application/json", "User-Agent": "nanobot-forensics"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # A non-200 says nothing about the payment. Never read it as "unpaid".
            return LookupOutcome("UNAVAILABLE",
                                 f"lookup returned HTTP {exc.code}; the endpoint may "
                                 "have moved or gained auth. Do not treat this as "
                                 "evidence either way.")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                json.JSONDecodeError) as exc:
            return LookupOutcome("ERROR", f"lookup failed: {type(exc).__name__}")

        body = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return LookupOutcome("UNAVAILABLE", "unexpected response shape")

        status = str(body.get("status", "")).upper()
        biller = str(body.get("billerName") or "").strip()
        amount = body.get("amount")
        payer = str(body.get("name") or "").strip()

        if status in _PAID:
            self._audit(digits, "VERIFIED")
            return LookupOutcome(
                "VERIFIED",
                f"payment settled at {biller or 'unknown biller'} for {amount}",
                {"biller": biller, "amount": amount, "payer": payer,
                 "status": status, "rrr": digits})
        if status in _UNPAID:
            return LookupOutcome(
                "NOT_PAID",
                f"{biller or 'unknown biller'} has an OPEN request for {amount}, "
                f"status {status}. The document is a payment request, not proof "
                "that anyone paid.",
                {"biller": biller, "amount": amount, "payer": payer,
                 "status": status, "rrr": digits})
        if payload.get("status") != _OK:
            return LookupOutcome(
                "NOT_FOUND",
                f"reference did not resolve ({payload.get('message')}). Expired "
                "invoices, cancelled references and non-Remita rails all look "
                "like this - refer to a human, do not accuse.",
                {"rrr": digits})
    def _audit(self, rrr: str, state: str) -> None:
        """Record reads of personal data. Failures are not logged: the log is a
        record of whose data was actually seen."""
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "rrr": rrr, "state": state}) + "\n")
        except OSError:
            pass
