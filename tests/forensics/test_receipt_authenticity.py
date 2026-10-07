"""Receipt authenticity rules, pinned to the exact samples they were derived from.

Every case here is a real receipt examined in this project. Genuine documents
MUST NOT be flagged - false positives on real receipts are what get fraud tools
turned off. Known fakes MUST be flagged.
"""
from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.receipt_authenticity import ReceiptAuthenticityTool
from nanobot.forensics.remita_ledger import RemitaLedger


async def _run(**kw) -> dict:
    return json.loads(await ReceiptAuthenticityTool().execute(**kw))


GENUINE = [
    pytest.param(dict(rail="opay", reference="261006020100941692514314",
                      session_id="100004261006044343173401664684",
                      printed_date="Oct 6th, 2026", printed_time="05:43:36"),
                 id="opay-transfer-2026"),
    pytest.param(dict(rail="opay", reference="251231020100845730685880",
                      session_id="100004251231115351148906685011",
                      printed_date="Dec 31st, 2025", printed_time="12:53:43"),
                 id="opay-transfer-2025"),
    pytest.param(dict(rail="kuda", reference="090267260923120053981083702730",
                      printed_date="September 23, 2026", printed_time="01:00 PM",
                      sender_account="2083702730"), id="kuda-2026-pm"),
    pytest.param(dict(rail="kuda", reference="090267260908075726128083702730",
                      printed_date="September 8, 2026", printed_time="08:57 AM",
                      sender_account="2083702730"), id="kuda-2026-am"),
    # 2022 template prints no time: clock rules must be NOT_APPLICABLE.
    pytest.param(dict(rail="kuda", reference="090267220308130543880005266245",
                      printed_date="08 MAR 2022"), id="kuda-2022-no-time"),
    pytest.param(dict(rail="access", reference="NXG000014230811022605280674573281",
                      printed_date="2023-08-11", printed_time="02:26:35"),
                 id="access-local-clock"),
    # GTBank prints GMT+0; the reference is local WAT.
    pytest.param(dict(rail="gtbank", reference="000013240109115854000078319435",
                      printed_date="9 Jan 2024, GMT+0", printed_time="10:54"),
                 id="gtbank-gmt0"),
    pytest.param(dict(rail="moniepoint",
                      reference="TRFI2MPT6khzl1881414650109931520",
                      printed_date="January 20th, 2025", printed_time="7:53 PM"),
                 id="moniepoint-snowflake"),
]

FAKE = [
    pytest.param(dict(rail="opay", reference="230803026525235648",
                      session_id="100004230803012514105527583994",
                      printed_date="Aug 04, 2023", printed_time="10:52"),
                 "OPAY-SESSION-CLOCK", id="opay-fake-506-minutes"),
    pytest.param(dict(rail="opay", reference="221231028393207772",
                      session_id="100004221231013706102217104375",
                      printed_date="Dec 3, 2022", printed_time="02:37"),
                 "OPAY-DATE", id="opay-fake-day-slip"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", GENUINE)
async def test_genuine_receipts_are_not_flagged(kw):
    out = await _run(**kw)
    assert out["state"] != "INVALID", f"false positive: {out['violations']}"


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,rule", FAKE)
async def test_known_fakes_are_caught(kw, rule):
    out = await _run(**kw)
    assert out["state"] == "INVALID"
    assert rule in {v["rule"] for v in out["violations"]}


@pytest.mark.asyncio
async def test_clean_local_result_is_never_verified():
    """Self-consistency is not proof of payment; the tool must say so."""
    out = await _run(rail="opay", reference="260127020100619544950457",
                     session_id="100004260127213139150916695873",
                     printed_date="Jan 27th, 2026", printed_time="22:31:14")
    assert out["state"] == "UNVERIFIED"
    assert "NOT proof of payment" in out["guidance"]


@pytest.mark.asyncio
async def test_remita_is_honest_about_not_being_self_proving():
    out = await _run(rail="remita", reference="1514-2163-2972")
    statuses = {c["rule"]: c["status"] for c in out["checks"]}
    assert statuses["REMITA-NO-SELF-PROOF"] == "not_applicable"
    assert out["state"] != "VERIFIED"


@pytest.mark.asyncio
async def test_vat_is_on_the_charge_not_the_principal():
    out = await _run(rail="remita", reference="1514-2163-2972",
                     amount=50000.0, charges=300.0, vat=22.5, total=15322.5,
                     printed_date="9 Jan 2024")
    assert "ARITH-TOTAL" in {v["rule"] for v in out["violations"]}


def test_ledger_refuses_unpresented_reference():
    """No probing of references nobody handed over, even if the rail is enabled."""
    led = RemitaLedger(enabled=True, min_interval=0)
    out = led.verify("151421632972", presented_by_claimant=False)
    assert out.state == "REFUSED"


def test_ledger_is_off_by_default():
    led = RemitaLedger()
    assert led.configured is False
    assert led.verify("151421632972", presented_by_claimant=True).state == "UNAVAILABLE"


def test_ledger_has_no_batch_or_sweep_surface():
    """Structural guard: nothing here can iterate references."""
    forbidden = {"batch", "lookup_many", "sweep", "iterate", "range", "scan",
                 "probe", "enumerate", "check_many"}
    surface = {n.lower() for n in dir(RemitaLedger)}
    assert not (surface & forbidden), surface & forbidden


def test_rail_inference_from_shape_only():
    t = ReceiptAuthenticityTool()
    assert t._guess_rail("NXG000014230811022605280674573281", False) == "access"
    assert t._guess_rail("090267260923120053981083702730", False) == "kuda"
    assert t._guess_rail("151421632972", False) == "remita"


@pytest.mark.asyncio
async def test_bill_payment_without_session_is_not_a_violation():
    """A genuine AEDC receipt prints no Session ID. Absence is only evidence
    within the transfer class - 'missing field' is never global proof."""
    out = await _run(rail="opay", reference="260908090100819678111430",
                     printed_date="Sep 8th, 2026", printed_time="16:51:43",
                     txn_class="bill-payment")
    assert out["state"] != "INVALID", out["violations"]
    statuses = {c["rule"]: c["status"] for c in out["checks"]}
    assert statuses["OPAY-SESSION"] == "not_applicable"


@pytest.mark.asyncio
async def test_transfer_without_session_is_still_flagged():
    out = await _run(rail="opay", reference="261006020100941692514314",
                     printed_date="Oct 6th, 2026", printed_time="05:43:36",
                     txn_class="transfer")
    assert "OPAY-SESSION" in {v["rule"] for v in out["violations"]}


@pytest.mark.asyncio
async def test_typography_catches_the_forgery_that_beats_every_clock_rule():
    """OPAY-F1 satisfied length, date, type, 0100, session prefix and the clock
    window. Only '₦43000.00' missing its separator exposed it."""
    out = await _run(rail="opay", reference="260127020100619544950457",
                     session_id="100004260127213139150916695873",
                     printed_date="Jan 27th, 2026", printed_time="22:31:14",
                     amount_text="₦43000.00")
    assert "XX-AMO" in {v["rule"] for v in out["violations"]}
    clean = await _run(rail="opay", reference="261006020100941692514314",
                       session_id="100004261006044343173401664684",
                       printed_date="Oct 6th, 2026", printed_time="05:43:36",
                       amount_text="₦300.00")
    assert clean["state"] != "INVALID"
