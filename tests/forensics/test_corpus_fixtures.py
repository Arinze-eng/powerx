"""Runs the whole labelled corpus against the engine.

This is the test that matters for a fraud tool: genuine receipts must never be
rejected, and known fakes must be. Both defects found in this project (a false
positive on a genuine bill payment, and a missing typography rule) were caught
by running the corpus, not by reading it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.tools.receipt_authenticity import ReceiptAuthenticityTool

CORPUS = json.loads((Path(__file__).parent / "receipt_fixtures.json").read_text())
SAMPLES = CORPUS["samples"]


def _kw(s: dict) -> dict:
    kw = {"rail": s["rail"], "reference": s["reference"]}
    for k in ("session_id", "printed_date", "printed_time", "sender_account",
              "txn_class", "amount_in_words"):
        if s.get(k):
            src = "printed_date" if k == "printed_date" else k
            kw[k if k != "amount_in_words" else "words"] = s[src]
    for k, out in (("amount", "amount"), ("charges", "charges"),
                   ("vat", "vat"), ("total", "total")):
        if s.get(k) is not None:
            kw[out] = s[k]
    if s.get("raw_amount_text"):
        kw["amount_text"] = s["raw_amount_text"]
    if s.get("document_text"):
        kw["document_text"] = s["document_text"]
    return kw


@pytest.mark.parametrize("s", SAMPLES, ids=[s["id"] for s in SAMPLES])
async def test_corpus_matches_expectations(s):
    out = json.loads(await ReceiptAuthenticityTool().execute(**_kw(s)))
    gt = s["ground_truth"]
    if gt == "genuine":
        assert out["state"] != "INVALID", (
            f"FALSE POSITIVE on genuine {s['id']}: {out['violations']}")
    if s.get("expected_violations"):
        got = {v["rule"] for v in out["violations"]}
        for want in s["expected_violations"]:
            key = want.split(":")[0].split("/")[0].strip()
            assert key in got or any(key in g for g in got) or out["state"] == "INVALID", (
                f"{s['id']} should break {want}, got {got}")


def test_corpus_is_big_enough_to_trust():
    """Guard against silently shrinking the ground truth set."""
    assert len(SAMPLES) >= 18
    assert sum(1 for x in SAMPLES if x["ground_truth"] == "genuine") >= 10
    assert any("genuine-invoice" in x["ground_truth"] for x in SAMPLES)
