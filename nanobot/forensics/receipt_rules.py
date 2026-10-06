"""Deterministic rules that catch fabricated Nigerian payment receipts.

Every rule here is derived from real receipts (OPay, Kuda, Access, GTBank,
Moniepoint, Remita) and each one returns PASS, FAIL, or NOT_APPLICABLE.

The load-bearing idea
---------------------
Nigerian payment rails stamp the *instant* a transfer was created into the
reference string, usually as ``YYMMDD`` + ``HHMMSS``. Receipts print that same
moment in local time. A forger who retypes the visible date or amount has to
rewrite the hidden stamp too - and cannot, because the reference carries
**seconds** while the page prints only **minutes**. That gap is unguessable, so
the two disagree, and the disagreement *is* the detection.

The same reasoning exposes what these rules cannot do. A receipt that satisfies
every check may still be a recycled reference (a real payment shown for the
wrong amount or the wrong beneficiary) or a genuinely generated invoice used as
proof of payment. Nothing here can see that; only the payee's own ledger can.
This module therefore reports ``INVALID`` or ``UNVERIFIED`` - never ``VERIFIED``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# The measured fact this whole module rests on
# ---------------------------------------------------------------------------
# Every rail stamps the payment instant into the reference, but NOT in the same
# frame, and getting this wrong rejects genuine receipts:
#
#   rail      embedded stamp      page prints        relationship
#   OPay      UTC  (04:43:43)     05:43:36 local     +7 s   after  (UTC+1)
#   Kuda      UTC  (12:00:53)     01:00 PM local     +53 s  after  (UTC+1, page rounds to min)
#   Access    local(02:26:05)     02:26:35 local     -30 s  before (no offset)
#   GTBank    local(11:58:54)     10:54 GMT+0        ~+5 min after (page declares UTC!)
#
# So: OPay/Kuda are UTC, Access/GTBank are local, and GTBank additionally prints
# its *display* time in GMT+0. There is no single correct assumption, which is
# why utc_embedded is a parameter and the GTBank case is handled in _as_datetime.
#
# Nigeria is UTC+1 year-round with no DST, so the offset itself is permanent.
WAT = timezone(timedelta(hours=1), "WAT")

#: Signed slack, in seconds, between an embedded stamp and the printed time.
#: Negative means the stamp is EARLIER than the page (initiation precedes
#: completion). Positive means later. Measured on genuine samples:
#:   OPay  +7 s / +8 s      Kuda  ~+53 s (page rounds to the minute)
#:   Access -30 s           GTBank +4.9 min
#: Fakes measured: +506 min (OPay), and a whole-day date slip. The window is
#: generous on purpose - false positives on real receipts are the failure mode
#: that gets a fraud tool switched off.
_MAX_SLACK = timedelta(minutes=10)
_MAX_EARLY = timedelta(minutes=10)

PASS, FAIL, NA = "pass", "fail", "not_applicable"


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status in (PASS, NA)


@dataclass
class ReceiptAssessment:
    rail: str
    results: list[RuleResult] = field(default_factory=list)

    @property
    def violations(self) -> list[RuleResult]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def state(self) -> str:
        return "INVALID" if self.violations else "UNVERIFIED"

    def to_dict(self) -> dict:
        return {
            "rail": self.rail,
            "state": self.state,
            "note": (
                "UNVERIFIED means no rule was contradicted, NOT that the payment "
                "happened. Recycled references and unpaid invoices are invisible "
                "to these checks; only the payee's own ledger can clear them."
            ),
            "violations": [
                {"rule": r.rule, "detail": r.detail} for r in self.violations
            ],
            "checks": [{"rule": r.rule, "status": r.status, "detail": r.detail}
                       for r in self.results],
        }


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _as_datetime(date_text: str, time_text: str = "") -> tuple[datetime | None, bool]:
    """Parse a printed date and optional time.

    Returns ``(moment, time_was_printed)``. The second flag matters: a receipt
    that shows only a date must yield NOT_APPLICABLE on clock rules, never a
    midnight comparison that fabricates a 14-hour contradiction and rejects a
    genuine receipt.
    """
    if not date_text:
        return None, False
    cleaned = re.sub(r"(?<=[\d])(st|nd|rd|th)", "", date_text.strip(), flags=re.I)
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                "%d/%m/%y", "%m/%d/%y", "%d/%m/%Y"):
        for cand in (cleaned, cleaned.split("|")[0].strip(),
                     re.split(r"\s+at\s+", cleaned)[0].strip()):
            try:
                base = datetime.strptime(cand, fmt)
            except ValueError:
                continue
            # Some receipts print their own timezone and mean it. GTBank prints
            # "9 Jan 2024 10:54, GMT+0" - a UTC wall clock, so comparing it raw
            # against a local NIP stamp is off by an hour and would have failed a
            # genuine receipt. Honour the declared zone instead of assuming WAT.
            blob = f"{date_text} {time_text}".lower()
            if "gmt+0" in blob or "utc" in blob or ("gmt" in blob and "gmt+1" not in blob):
                base += timedelta(hours=1)
            # Merge the printed clock in. Returning the date at midnight here was
            # a real bug: it made a genuine 19:53 receipt compare as 00:00 and
            # "contradict" its own reference by 20 hours.
            t = _as_time(time_text)
            if t:
                base = base.replace(hour=t.hour, minute=t.minute, second=t.second)
                return base, True
            return base, False
    if time_text:
        base, _ = _as_datetime(date_text, "")
        t = _as_time(time_text)
        if base and t:
            return base.replace(hour=t.hour, minute=t.minute, second=t.second), True
    return None, False


def _as_time(time_text: str) -> datetime | None:
    t = (time_text or "").strip()
    if not t:
        return None
    t = t.replace(".", ":").strip()
    for fmt in ("%H:%M:%S %p", "%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S",
                "%I:%M%p", "%H %M"):
        try:
            return datetime.strptime(t.replace("  ", " "), fmt)
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
        if ap == "am" and hour == 12:
            hour = 0
    elif hour > 12:
        ap = None
    try:
        return datetime(2000, 1, 1, hour, minute, int(ss))
    except ValueError:
        return None


def _stamp_vs_printed(rule: str, stamp: str, printed: datetime | None,
                      label: str = "embedded",
                      utc_embedded: bool = True,
                      has_time: bool = True) -> RuleResult:
    """Compare a 6-digit HHMMSS stamp against the printed wall clock.

    ``utc_embedded`` is not cosmetic - it is measured per rail, and getting it
    wrong fails genuine receipts. OPay and Kuda stamp the NIP instant in **UTC**
    (Kuda 12:00:53Z against a page printing 01:00 PM; OPay session 04:43:43Z
    against 05:43:36). Access and GTBank stamp **local** time (Access 02:26:05
    against a printed 02:26:35). One shared assumption would misjudge both.
    """
    if printed is None or not has_time:
        return RuleResult(rule, NA, f"{label} {stamp}: receipt prints no time, "
                                    "so the clock cannot be cross-checked")
    try:
        wat = datetime.strptime(stamp, "%H%M%S").replace(
            year=printed.year, month=printed.month, day=printed.day)
        if utc_embedded:
            wat += timedelta(hours=1)
    except ValueError:
        return RuleResult(rule, FAIL,
                          f"{label} clock '{stamp}' is not a valid HHMMSS")
    delta = wat - printed
    if delta > _MAX_SLACK:
        return RuleResult(rule, FAIL,
                          f"{label} {stamp} is {delta.total_seconds()/60:.1f} min "
                          f"LATER than printed {printed:%H:%M:%S}; genuine stamps "
                          "sit within seconds-to-minutes of the page")
    if delta < -_MAX_EARLY:
        return RuleResult(rule, FAIL,
                          f"{label} {stamp} is {-delta.total_seconds()/60:.1f} min "
                          f"EARLIER than printed {printed:%H:%M:%S}")
    return RuleResult(rule, PASS,
                      f"{label} {stamp} agrees with printed {printed:%H:%M:%S} "
                      f"({delta.total_seconds():+.0f} s)")


def _date_check(rule: str, stamp: str, printed: datetime | None,
                source: str) -> RuleResult:
    if printed is None:
        return RuleResult(rule, NA, f"{source} embeds {stamp}; printed date "
                                     "unparsed, cannot compare")
    want = printed.strftime("%y%m%d")
    if stamp == want:
        return RuleResult(rule, PASS, f"{source} date {stamp} matches printed {want}")
    return RuleResult(rule, FAIL,
                      f"{source} embeds {stamp} but the receipt prints {want} - "
                      "the reference and the page disagree on the day")


def _len_check(rule: str, ref: str, want: int) -> RuleResult:
    """Length is a TEMPLATE property, and templates drift.

    A 2022 OPay reference is 18 digits where a 2026 one is 24. Reporting that as
    fraud would reject genuine old receipts, so a mismatch is NOT_APPLICABLE:
    worth a reviewer's eye, not a verdict. The date/clock cross-checks above are
    era-proof because they compare a receipt against itself.
    """
    d = _digits(ref)
    if len(d) == want:
        return RuleResult(rule, PASS, f"reference length {len(d)} as expected")
    return RuleResult(
        rule, NA,
        f"reference is {len(d)} digits, expected {want}; rail formats change over "
        "the years, so this is flagged for review rather than called fraudulent")


def assess_opay(reference: str = "", session_id: str = "",
                printed_date: str = "", printed_time: str = "") -> ReceiptAssessment:
    a = ReceiptAssessment(rail="opay")
    printed, has_time = _as_datetime(printed_date, printed_time)
    ref, sess = _digits(reference), _digits(session_id)
    if not ref:
        a.results.append(RuleResult("OPAY-REFERENCE", NA, "no reference supplied"))
        return a

    a.results.append(_len_check("OPAY-LEN", ref, 24))
    if len(ref) >= 6:
        a.results.append(_date_check("OPAY-DATE", ref[0:6], printed, "reference"))
    if len(ref) >= 8 and ref[6:8] not in ("02", "09"):
        a.results.append(RuleResult("OPAY-TYPE", FAIL,
                                    f"transaction-type block {ref[6:8]} unseen "
                                    "(02=transfer, 09=bill)"))
    elif len(ref) >= 8:
        a.results.append(RuleResult("OPAY-TYPE", PASS, f"type block {ref[6:8]}"))

    if not sess:
        a.results.append(RuleResult(
            "OPAY-SESSION", FAIL,
            "a bank transfer carries no Session ID; genuine OPay transfers "
            "always print one (bill payments may not)"))
        return a

    sd = sess
    if len(sd) >= 18 and sd[:5] == "10000" and sd[5] == "4":
        a.results.append(RuleResult("OPAY-SESSION-FMT", PASS, "session prefix 10000|4"))
        a.results.append(_date_check("OPAY-SESSION-DATE", sd[6:12], printed, "session"))
        a.results.append(_stamp_vs_printed("OPAY-SESSION-CLOCK", sd[12:18], printed,
                                           "session", has_time=has_time))
        if len(ref) >= 6 and sd[6:12] != ref[0:6]:
            a.results.append(RuleResult(
                "OPAY-TWO-CLOCKS", FAIL,
                f"reference date {ref[0:6]} and session date {sd[6:12]} disagree; "
                "a genuine receipt's two stamps are generated together"))
        else:
            a.results.append(RuleResult("OPAY-TWO-CLOCKS", PASS,
                                         "reference and session stamps agree"))
    else:
        a.results.append(RuleResult("OPAY-SESSION-FMT", FAIL,
                                    f"session '{sess[:10]}...' does not start 10000|4"))
    return a


def assess_kuda(reference: str = "", printed_date: str = "",
                printed_time: str = "", sender_account: str = "") -> ReceiptAssessment:
    a = ReceiptAssessment(rail="kuda")
    printed, has_time = _as_datetime(printed_date, printed_time)
    ref = _digits(reference)
    if not ref:
        a.results.append(RuleResult("KUDA-REFERENCE", NA, "no reference supplied"))
        return a

    a.results.append(_len_check("KUDA-LEN", ref, 30))
    if len(ref) >= 12:
        a.results.append(_date_check("KUDA-DATE", ref[6:12], printed, "reference"))
    if len(ref) >= 18:
        a.results.append(_stamp_vs_printed("KUDA-CLOCK", ref[12:18], printed,
                                           "reference", has_time=has_time))
    if len(ref) >= 28 and sender_account:
        tail, acct = _digits(ref[-10:]), _digits(sender_account)
        if acct and tail[-9:] != acct[-9:]:
            a.results.append(RuleResult(
                "KUDA-ACCOUNT", FAIL,
                f"reference tail {tail} does not carry the sender account {acct}"))
        else:
            a.results.append(RuleResult("KUDA-ACCOUNT", PASS,
                                        "reference tail carries the sender account"))
    return a


def assess_nip(style: str, reference: str = "", printed_date: str = "",
               printed_time: str = "") -> ReceiptAssessment:
    """Access / GTBank style: digits, then serial|YYMMDD|HHMMSS|remainder."""
    a = ReceiptAssessment(rail=style)
    printed, has_time = _as_datetime(printed_date, printed_time)
    d = _digits(reference)
    if len(d) < 18:
        a.results.append(RuleResult(f"{style.upper()}-REFERENCE", NA,
                                    f"only {len(d)} digits; format not recognised"))
        return a
    a.results.append(_date_check(f"{style.upper()}-DATE", d[6:12], printed, "reference"))
    a.results.append(_stamp_vs_printed(f"{style.upper()}-CLOCK", d[12:18], printed,
                                       "reference", utc_embedded=False,
                                       has_time=has_time))
    return a


def assess_remita(rrr: str = "", printed_date: str = "") -> ReceiptAssessment:
    """A Remita RRR carries no date and no check digit - say so plainly."""
    a = ReceiptAssessment(rail="remita")
    d = _digits(rrr)
    if not d:
        a.results.append(RuleResult("REMITA-RRR", NA, "no RRR supplied"))
        return a
    if len(d) != 12:
        a.results.append(RuleResult("REMITA-RRR", FAIL,
                                    f"RRR has {len(d)} digits, expected 12"))
    else:
        a.results.append(RuleResult("REMITA-RRR", PASS, "RRR is 12 digits"))
    a.results.append(RuleResult(
        "REMITA-NO-SELF-PROOF", NA,
        "An RRR encodes no date, time or check digit, so NO local rule can prove "
        "payment occurred. This reference must be cleared against the payee's "
        "Remita ledger; a passing format here is not evidence of payment."))
    return a
