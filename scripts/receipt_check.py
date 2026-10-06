#!/usr/bin/env python3
"""
receipt_check.py - Nigerian payment receipt verifier (OPay, Kuda, Access,
GTBank, Moniepoint, Remita). Standalone, stdlib only, no API keys needed.

THE PRINCIPLE
  Every Nigerian rail stamps the moment a transfer was created into the
  reference string. OPay and Kuda store that stamp in UTC; Access and GTBank
  store it in local time. The receipt page prints only MINUTES while the
  reference carries SECONDS - so a forger who retypes the visible date or time
  cannot reconstruct the hidden stamp, and the two disagree. That gap is the
  detector. No machine learning, no image analysis, no credentials.

WHAT THIS CANNOT DO
  Local analysis can prove a receipt is WRONG. It can never prove a payment
  HAPPENED. Two frauds pass every check here:
    1. Recycled reference - a genuine payment shown for a different amount or
       beneficiary.
    2. Unpaid invoice - a real, self-consistent payment *request*. Remita even
       prints "This is not a Receipt" because this scam is so common.
  Only the payee's own ledger settles those. Hence: this tool reports INVALID
  or UNVERIFIED, and never "genuine".

USAGE
  python3 receipt_check.py --rail opay --reference 261006020100941692514314 \
      --session 100004261006044343173401664684 --date "Oct 6th, 2026" --time 05:43:36

  python3 receipt_check.py --auto --reference "090267260923120053981083702730" \
      --date "September 23, 2026" --time "01:00 PM" --account 2083702730

  python3 receipt_check.py --rail remita --reference 151421632972 \
      --ledger --presented      # asks Remita if it was actually paid

  Exit codes: 0 = no contradiction, 1 = INVALID (contradicts itself), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- constants ---

WAT = timezone(timedelta(hours=1))     # Nigeria: UTC+1 all year, no DST

# Measured, not assumed. Getting this wrong rejects genuine receipts.
UTC_STAMPS = {"opay", "kuda"}
LOCAL_STAMPS = {"access", "gtbank"}

# Signed slack between an embedded stamp and the printed clock.
SLACK_LATE = timedelta(minutes=10)
SLACK_EARLY = timedelta(minutes=10)

# Moniepoint tails are Snowflake IDs: (id >> 22) + epoch_ms = creation time.
SNOWFLAKE_EPOCH_MS = 1_288_834_974_657

RRR_URL = os.environ.get(
    "REMITA_GUEST_LOOKUP_URL",
    "https://api-remitacenta-login.remita.net/services/guestbridge-service"
    "/api/v1/biller/lookup/{rrr}",
)
RRR_ENABLED = os.environ.get("REMITA_LEDGER", "").strip().lower() in {
    "1", "true", "yes", "on"}
RRR_MIN_INTERVAL = float(os.environ.get("REMITA_MIN_INTERVAL", "2.0"))
RRR_AUDIT = os.environ.get("REMITA_AUDIT_LOG", "remita_lookup_audit.jsonl")

INVOICE_CUES = ("this is not a receipt", "amount payable", "payable in respect",
                "e-invoice", "invoice", "kindly pay", "to pay at any", "payable")
PAID_CUES = ("successful", "confirmed", "received by bank", "paid on", "debit",
             "transaction status", "settled")
PAID_STATES = {"SUCCESS", "SUCCESSFUL", "PAID", "CONFIRMED", "COMPLETED", "DEBITED"}
OPEN_STATES = {"PENDING", "UNPAID", "NOT_PAID", "INITIATED", "GENERATED", "CREATED"}

PASS, FAIL, NA = "pass", "fail", "not_applicable"

# Observed genuine behaviour, for the printed summary.
GENUINE_BASELINE = {
    "opay": "session clock lands ~+7 to +8 s after the printed time (UTC+1)",
    "kuda": "reference clock lands ~+53 s after a minute-rounded printed time",
    "access": "reference clock lands ~-30 s before the printed time (local, no offset)",
    "gtbank": "reference clock lands ~+5 min after; page may print GMT+0",
    "moniepoint": "19-digit tail decodes to the exact printed minute",
    "remita": "RRR encodes NO date, time or check digit - cannot self-prove",
}


# ---------------------------------------------------------------- primitives --

@dataclass
class Check:
    rule: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    rail: str
    reference: str
    checks: list[Check] = field(default_factory=list)

    @property
    def violations(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def state(self) -> str:
        return "INVALID" if self.violations else "UNVERIFIED"

    def add(self, rule, status, detail):
        self.checks.append(Check(rule, status, detail))


def digits(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def parse_date(text: str, time_text: str = ""):
    """Return (moment, time_was_printed). No time => NOT_APPLICABLE, never
    midnight: comparing against 00:00 fabricates contradictions and fails real
    receipts, especially older templates that print no clock at all."""
    if not text:
        return None, False
    cleaned = re.sub(r"(?<=[\d])(st|nd|rd|th)", "", text.strip(), flags=re.I)
    blob = f"{text} {time_text}".lower()
    declares_utc = ("gmt+0" in blob or "utc" in blob
                    or ("gmt" in blob and "gmt+1" not in blob))
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                "%d/%m/%y", "%m/%d/%y", "%d/%m/%Y"):
        for cand in (cleaned, cleaned.split("|")[0].strip(),
                     re.split(r"\s+at\s+", cleaned)[0].strip()):
            try:
                base = datetime.strptime(cand, fmt)
            except ValueError:
                continue
            if declares_utc:            # GTBank prints GMT+0 and means it
                base += timedelta(hours=1)
            t = parse_time(time_text)
            if t:
                return base.replace(hour=t.hour, minute=t.minute,
                                    second=t.second), True
            return base, False
    return None, False


def parse_time(text: str):
    t = (text or "").strip().replace(".", ":")
    if not t:
        return None
    for fmt in ("%H:%M:%S %p", "%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M",
                "%I:%M%p"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?", t)
    if not m:
        return None
    hh, mm, ss, ap = m.group(1), m.group(2), m.group(3) or "0", m.group(4)
    hour, minute = int(hh), int(mm)
    if ap:
        ap = ap.lower()
        if ap == "pm" and hour != 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
    try:
        return datetime(2000, 1, 1, hour, minute, int(ss))
    except ValueError:
        return None


# ------------------------------------------------------------- shared checks --

def stamp_check(rep: Report, rule: str, stamp: str, printed, has_time: bool,
                rail: str, label: str) -> None:
    if printed is None:
        rep.add(rule, NA, f"{label} {stamp}: could not read the printed date")
        return
    if not has_time:
        rep.add(rule, NA, f"{label} {stamp}: receipt prints NO time, so the clock "
                          "cannot be cross-checked (older templates omit it)")
        return
    try:
        t = datetime.strptime(stamp, "%H%M%S").replace(
            year=printed.year, month=printed.month, day=printed.day)
    except ValueError:
        rep.add(rule, FAIL, f"{label} clock '{stamp}' is not a valid HHMMSS")
        return
    if rail in UTC_STAMPS:
        t += timedelta(hours=1)
    delta = t - printed
    if delta > SLACK_LATE:
        rep.add(rule, FAIL, f"{label} {stamp} is {delta.total_seconds()/60:.1f} min "
                            f"LATER than printed {printed:%H:%M:%S}")
    elif delta < -SLACK_EARLY:
        rep.add(rule, FAIL, f"{label} {stamp} is {-delta.total_seconds()/60:.1f} min "
                            f"EARLIER than printed {printed:%H:%M:%S}")
    else:
        rep.add(rule, PASS, f"{label} {stamp} agrees with printed "
                            f"{printed:%H:%M:%S} ({delta.total_seconds():+.0f} s)")


def date_check(rep: Report, rule: str, stamp: str, printed, label: str) -> None:
    if printed is None:
        rep.add(rule, NA, f"{label} {stamp}: printed date unreadable")
        return
    want = printed.strftime("%y%m%d")
    if stamp == want:
        rep.add(rule, PASS, f"{label} date {stamp} matches printed {want}")
    else:
        rep.add(rule, FAIL, f"{label} embeds {stamp} but page prints {want} - "
                            "reference and page disagree on the day")


def length_check(rep: Report, rule: str, d: str, want: int) -> None:
    """Length is a TEMPLATE property and templates drift (OPay: 18 digits in
    2022, 24 in 2026). Mismatch is review, never verdict."""
    if len(d) == want:
        rep.add(rule, PASS, f"reference length {len(d)} as expected")
    else:
        rep.add(rule, NA, f"reference is {len(d)} digits, expected {want}; rail "
                          "formats change over the years, so this is a note for a "
                          "reviewer, not a finding of fraud")


# ------------------------------------------------------------------- rails ----

def check_opay(rep: Report, ref: str, session: str, printed, has_time: bool):
    d = digits(ref)
    length_check(rep, "OPAY-LEN", d, 24)
    if len(d) >= 6:
        date_check(rep, "OPAY-DATE", d[0:6], printed, "reference")
    if len(d) >= 8:
        if d[6:8] in ("02", "09"):
            rep.add("OPAY-TYPE", PASS, f"transaction-type block {d[6:8]}")
        else:
            rep.add("OPAY-TYPE", NA, f"type block {d[6:8]} unseen (02=transfer, "
                                     "09=bill); older formats may differ")
    if not session:
        rep.add("OPAY-SESSION", FAIL,
                "no Session ID on a bank transfer: genuine OPay transfers always "
                "print one (bill payments legitimately do not)")
        return
    s = digits(session)
    if len(s) < 18 or s[:5] != "10000" or s[5] != "4":
        rep.add("OPAY-SESSION-FMT", FAIL,
                f"session '{s[:12]}' does not begin 10000|4")
        return
    rep.add("OPAY-SESSION-FMT", PASS, "session prefix 10000|4, 30 digits")
    date_check(rep, "OPAY-SESSION-DATE", s[6:12], printed, "session")
    stamp_check(rep, "OPAY-SESSION-CLOCK", s[12:18], printed, has_time, "opay",
                "session")
    if len(d) >= 6:
        if s[6:12] == d[0:6]:
            rep.add("OPAY-TWO-CLOCKS", PASS,
                    "reference and session stamps agree (two independent copies "
                    "of the same instant)")
        else:
            rep.add("OPAY-TWO-CLOCKS", FAIL,
                    f"reference says {d[0:6]} but session says {s[6:12]} - a real "
                    "receipt generates both stamps together")


def check_kuda(rep: Report, ref: str, printed, has_time: bool, account: str):
    d = digits(ref)
    length_check(rep, "KUDA-LEN", d, 30)
    if d[:6] == "090267":
        rep.add("KUDA-PREFIX", PASS, "channel prefix 090267 (stable 2022->2026)")
    else:
        rep.add("KUDA-PREFIX", NA, f"prefix {d[:6]} (observed 090267)")
    if len(d) >= 12:
        date_check(rep, "KUDA-DATE", d[6:12], printed, "reference")
    if len(d) >= 18:
        stamp_check(rep, "KUDA-CLOCK", d[12:18], printed, has_time, "kuda",
                    "reference")
    if len(d) >= 30 and account:
        tail, acct = d[-10:], digits(account)
        if acct and tail[-9:] == acct[-9:]:
            rep.add("KUDA-ACCOUNT", PASS,
                    f"reference tail carries the sender account ({acct})")
        else:
            rep.add("KUDA-ACCOUNT", FAIL,
                    f"reference tail {tail} does not carry sender account {acct}")
    elif account:
        rep.add("KUDA-ACCOUNT", NA, "reference too short for an account cross-check")


def check_nip(rep: Report, style: str, ref: str, printed, has_time: bool):
    """Access / GTBank: digits, then serial | YYMMDD | HHMMSS | remainder.
    These rails stamp LOCAL time, so no UTC offset is applied."""
    d = digits(ref)
    if len(d) < 18:
        rep.add(f"{style.upper()}-REF", NA,
                f"only {len(d)} digits; format not recognised, cannot check")
        return
    rep.add(f"{style.upper()}-SERIAL", PASS,
            f"serial {d[0:6]} | date {d[6:12]} | clock {d[12:18]} | {d[18:]}")
    date_check(rep, f"{style.upper()}-DATE", d[6:12], printed, "reference")
    stamp_check(rep, f"{style.upper()}-CLOCK", d[12:18], printed, has_time,
                style, "reference")


def check_moniepoint(rep: Report, ref: str, printed, has_time: bool):
    """Alphanumeric reference, but the 19-digit tail is a Snowflake ID and
    decodes to the creation instant - 'opaque' does not mean unverifiable."""
    big = re.findall(r"\d{17,20}", ref)
    if not big:
        rep.add("MP-SNOWFLAKE", NA,
                "no 19-digit tail found, so no timestamp can be decoded")
        return
    ms = (int(big[0]) >> 22) + SNOWFLAKE_EPOCH_MS
    try:
        wat = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(WAT)
    except (OverflowError, OSError, ValueError):
        rep.add("MP-SNOWFLAKE", NA, "tail did not decode as a timestamp")
        return
    if printed is None or not has_time:
        rep.add("MP-SNOWFLAKE", NA,
                f"tail decodes to {wat:%Y-%m-%d %H:%M:%S} WAT; receipt gives no "
                "time to compare against")
        return
    naive = wat.replace(tzinfo=None)
    same_day = naive.strftime("%Y-%m-%d") == printed.strftime("%Y-%m-%d")
    same2 = printed.replace(year=naive.year, month=naive.month, day=naive.day)
    close = abs((naive - same2).total_seconds()) <= 120
    if same_day and close:
        rep.add("MP-SNOWFLAKE", PASS,
                f"tail decodes to {naive:%Y-%m-%d %H:%M:%S}, matches the printed "
                f"{printed:%Y-%m-%d %H:%M:%S}")
    else:
        rep.add("MP-SNOWFLAKE", FAIL,
                f"tail decodes to {naive:%Y-%m-%d %H:%M:%S} but the receipt prints "
                f"{printed:%Y-%m-%d %H:%M:%S}")
    if re.search(r"20\d{6}", digits(ref[:12])):
        rep.add("MP-DOUBLE-DATE", NA,
                "reference carries BOTH a human date and a snowflake timestamp; "
                "observed genuine references stamp the time once")


def check_remita(rep: Report, rrr: str):
    d = digits(rrr)
    if len(d) != 12:
        rep.add("REMITA-FORMAT", FAIL, f"RRR has {len(d)} digits, expected 12")
    else:
        rep.add("REMITA-FORMAT", PASS, f"RRR is 12 digits ({d[:2]} + {d[2:]} counter)")
    rep.add("REMITA-NO-SELF-PROOF", NA,
            "an RRR encodes no date, time or check digit, so NO local rule can "
            "prove payment. Clear it against the payee's Remita ledger.")


# ------------------------------------------------------------ cross-cutting ---

def check_document_type(rep: Report, text: str):
    """The most common fraud is not a forgery - it is a genuine INVOICE presented
    as proof of payment. Free to check, and it catches documents that are
    perfectly authentic and prove nothing."""
    if not text:
        rep.add("DOC-TYPE", NA, "no document text supplied; cannot tell whether "
                                "this is an invoice or a receipt")
        return
    low = text.lower()
    inv = [c for c in INVOICE_CUES if c in low]
    paid = [c for c in PAID_CUES if c in low]
    if "this is not a receipt" in low:
        rep.add("DOC-TYPE", FAIL,
                f"document self-declares 'This is not a Receipt' (request language: "
                f"{', '.join(inv[:3])}) - it is a payment request, not proof of payment")
        return
    if inv and not paid:
        rep.add("DOC-TYPE", FAIL,
                f"request language only ({', '.join(inv[:3])}): nothing here says "
                "the money moved")
    elif paid and not inv:
        rep.add("DOC-TYPE", PASS, f"settlement language present ({', '.join(paid[:3])})")
    elif inv and paid:
        rep.add("DOC-TYPE", NA,
                f"mixed wording: request {inv[:2]} alongside settlement {paid[:2]}; "
                "confirm a status/debit line really exists")
    else:
        rep.add("DOC-TYPE", NA, "no recognisable payment language in the text")


def check_arithmetic(rep: Report, amount, charges, vat, total, words):
    """Nigerian VAT applies to the SERVICE CHARGE only, never the principal.
    Lazy forgers change one field and leave the others."""
    if None not in (amount, charges, vat, total):
        exp_vat = round(float(charges) * 0.075, 2)
        rep.add("ARITH-VAT", PASS if abs(exp_vat - float(vat)) < 0.005 else FAIL,
                f"7.5% of charge {float(charges):,.2f} = {exp_vat:,.2f}; "
                f"document prints {float(vat):,.2f}")
        exp_total = round(float(amount) + float(charges) + exp_vat, 2)
        rep.add("ARITH-TOTAL", PASS if abs(exp_total - float(total)) < 0.005 else FAIL,
                f"principal+charge+VAT = {exp_total:,.2f}; document prints "
                f"{float(total):,.2f}")
    else:
        rep.add("ARITH-TOTAL", NA,
                "pass --amount --charges --vat --total to validate the money block")
    if words and total is not None:
        rep.add("ARITH-WORDS", NA,
                f"check by eye that the words describe {float(total):,.2f} "
                "(forgers edit digits and forget the spelled amount)")


def infer_rail(ref: str, has_session: bool) -> str:
    a = "".join(c for c in ref if not c.isdigit()).upper()
    d = digits(ref)
    if a.startswith("NXG"):
        return "access"
    if has_session or (len(d) == 24 and d.isdigit()):
        return "opay"
    if d[:6] == "090267":
        return "kuda"
    if "MPT" in a:
        return "moniepoint"
    if len(d) == 12 and not a:
        return "remita"
    if len(d) == 30:
        return "gtbank"
    return ""


# ------------------------------------------------------------------ ledger ----

def remita_ledger(rrr: str, presented: bool) -> dict:
    """Ask Remita whether the reference was actually paid.

    Guardrails, because each answer is a named person's financial position and
    RRRs are a short prefix plus a COUNTER, so neighbours resolve to neighbours:
      * one reference per run, by design - there is no batch mode to add
      * refuses anything nobody presented (probing = enumeration, not checking)
      * rate limited, and successful reads are audit-logged
      * off unless REMITA_LEDGER=1
    Prefer Remita's documented contractor API for production: this is the
    frontend endpoint and it can gain auth or move without notice.
    """
    if not presented:
        return {"state": "REFUSED",
                "detail": "no --presented flag. Querying references that nobody "
                          "handed you is enumeration of other people's payment "
                          "records, not verification."}
    if not RRR_ENABLED:
        return {"state": "UNAVAILABLE",
                "detail": "ledger disabled (set REMITA_LEDGER=1 only with the "
                          "institution's agreement). Report UNVERIFIED, never "
                          "VERIFIED, until then."}
    d = digits(rrr)
    if len(d) != 12:
        return {"state": "ERROR", "detail": "not a 12-digit RRR"}
    time.sleep(max(RRR_MIN_INTERVAL, 0.5))
    try:
        req = urllib.request.Request(RRR_URL.format(rrr=d), headers={
            "Accept": "application/json", "User-Agent": "receipt-check/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # A non-200 says nothing about the payment. Never read it as "unpaid".
        return {"state": "UNAVAILABLE",
                "detail": f"HTTP {e.code}; endpoint may have moved. Do not treat "
                          "as evidence either way."}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"state": "ERROR", "detail": f"{type(e).__name__}: {e}"}

    body = payload.get("data") or {}
    st = str(body.get("status", "")).upper()
    info = {"biller": body.get("billerName"), "amount": body.get("amount"),
            "payer": body.get("name"), "email": body.get("email"), "status": st}
    if st in PAID_STATES:
        _audit(d, "VERIFIED")
        return {"state": "VERIFIED",
                "detail": f"settled at {info['biller']} for {info['amount']}",
                "fields": info}
    if st in OPEN_STATES:
        return {"state": "NOT_PAID", "fields": info,
                "detail": f"{info['biller']} has an OPEN request for "
                          f"{info['amount']} ({st}) - a payment request, not proof "
                          "that anyone paid"}
    return {"state": "NOT_FOUND", "detail":
            f"did not resolve ({payload.get('message')}). Expired, cancelled and "
            "non-Remita references all look like this: refer to a human, do not "
            "accuse."}


def _audit(rrr: str, state: str) -> None:
    """Log reads of personal data. Failures are not logged: the file is a record
    of whose data was actually seen."""
    try:
        with open(RRR_AUDIT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"), "rrr": rrr, "state": state}) + "\n")
    except OSError:
        pass


# -------------------------------------------------------------------- main ----

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Check a Nigerian payment receipt for internal contradictions.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--rail", choices=sorted(UTC_STAMPS | LOCAL_STAMPS |
                                            {"moniepoint", "remita"}))
    g.add_argument("--auto", action="store_true",
                   help="infer the rail from the reference shape")
    p.add_argument("--reference", required=True,
                   help="Transaction No. / Reference / RRR, copied exactly")
    p.add_argument("--session", default="", help="OPay Session ID")
    p.add_argument("--date", default="", help="printed date, as shown")
    p.add_argument("--time", default="", help="printed time, as shown")
    p.add_argument("--account", default="", help="sender account (Kuda cross-check)")
    p.add_argument("--amount", type=float), p.add_argument("--charges", type=float)
    p.add_argument("--vat", type=float), p.add_argument("--total", type=float)
    p.add_argument("--words", default="", help="amount written out in words")
    p.add_argument("--text", default="", help="document text (invoice/receipt check)")
    p.add_argument("--file", default="", help="read document text from a file")
    p.add_argument("--ledger", action="store_true",
                   help="also ask Remita if the RRR was actually paid")
    p.add_argument("--presented", action="store_true",
                   help="assert the claimant handed over this reference")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    text = a.text
    if a.file:
        try:
            text = open(a.file, encoding="utf-8", errors="replace").read()
        except OSError as e:
            print(f"cannot read {a.file}: {e}", file=sys.stderr)
            return 2

    rail = a.rail or infer_rail(a.reference, bool(a.session))
    if not rail:
        print("Cannot infer the rail. Pass --rail (opay/kuda/access/gtbank/"
              "moniepoint/remita). Format rules are per-rail; a wrong guess "
              "false-positives on real receipts.", file=sys.stderr)
        return 2

    printed, has_time = parse_date(a.date, a.time)
    rep = Report(rail=rail, reference=a.reference)

    if rail == "opay":
        check_opay(rep, a.reference, a.session, printed, has_time)
    elif rail == "kuda":
        check_kuda(rep, a.reference, printed, has_time, a.account)
    elif rail in ("access", "gtbank"):
        check_nip(rep, rail, a.reference, printed, has_time)
    elif rail == "moniepoint":
        check_moniepoint(rep, a.reference, printed, has_time)
    elif rail == "remita":
        check_remita(rep, a.reference)

    check_document_type(rep, text)
    check_arithmetic(rep, a.amount, a.charges, a.vat, a.total, a.words)

    ledger = None
    if a.ledger:
        if rail != "remita":
            ledger = {"state": "UNAVAILABLE",
                      "detail": "ledger confirmation is wired for Remita only"}
        else:
            ledger = remita_ledger(a.reference, a.presented)
            if ledger["state"] == "NOT_PAID":
                rep.add("REMITA-NOT-PAID", FAIL, ledger["detail"])
            elif ledger["state"] == "VERIFIED":
                rep.add("REMITA-VERIFIED", PASS, ledger["detail"])

    state = rep.state
    if ledger and ledger["state"] == "VERIFIED":
        state = "VERIFIED"

    if a.json:
        print(json.dumps({
            "rail": rail, "reference": a.reference, "state": state,
            "violations": [{"rule": c.rule, "detail": c.detail}
                           for c in rep.violations],
            "checks": [{"rule": c.rule, "status": c.status, "detail": c.detail}
                       for c in rep.checks],
            "ledger": ledger,
        }, indent=2, default=str))
    else:
        banner = {"INVALID": "*** INVALID - the document contradicts itself ***",
                  "UNVERIFIED": "UNVERIFIED - nothing contradicts itself, but "
                                "payment is NOT proven",
                  "VERIFIED": "VERIFIED - settled, confirmed by Remita"}[state]
        print(f"\n  rail: {rail}   reference: {a.reference}")
        print(f"  {banner}\n")
        for c in rep.checks:
            tag = {"pass": " OK ", "fail": "FAIL", NA: " n/a"}[c.status]
            print(f"   [{tag}] {c.rule}: {c.detail}")
        if ledger:
            print(f"\n   ledger: {ledger['state']} - {ledger['detail']}")
        print(f"\n   {rail} baseline: {GENUINE_BASELINE.get(rail, '-')}")
        if state == "UNVERIFIED":
            print("\n   NEXT: a clean local result still does not prove payment.")
            print("   Recycle-check the reference across all past claims, and")
            print("   confirm against the payee's own ledger before accepting it.")
    return 1 if state == "INVALID" else 0


if __name__ == "__main__":
    sys.exit(main())
