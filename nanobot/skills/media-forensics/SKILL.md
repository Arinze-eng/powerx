---
name: media-forensics
description: Decide whether an image or document has been edited, and when it was taken. Use for receipts, payment screenshots, IDs, scans, contracts, and any "is this real / was this photoshopped / when was this taken" question.
---

# Media forensics — receipts, documents, and edited images

Use `media_forensics` when the user asks whether a **receipt, payment screenshot,
invoice, ID, scan, or photo is fake or edited**, or **when it was taken**. There is
also a `document-images` skill for *placing* images into documents; this one is for
*judging* them.

## The one rule

**Never tell the user a file is genuine.** The tool refuses to either, and so must
you. Nothing observable in a file proves an image was not fabricated, and a wrong
*"it's real"* on a receipt is the expensive failure — it is what gets a fraudulent
claim paid. The strongest honest sentence is *"I found no sign of editing, and here
is what that does and does not rule out."*

Corollary: never present a clean report as proof, and never let a user talk you
into a verdict the evidence does not support.

## Start here

```
media_forensics(action="analyze", path="receipt.jpg",
                expected_amount="275000", expected_date="2026-09-27")
```

Then **ask the user for the issuer's record** if they have it. `expected_amount` (plus
`expected_date` or `expected_reference`) is not optional garnish: it is the only input
that can actually settle authenticity, because it checks the document against the
bank's or merchant's own record rather than against pixels. Push for it.

## Actions

| Action | Use it for |
|---|---|
| `analyze` | The default. Image + document checks + verdict + artifact paths |
| `timestamps` | Just "when was this taken?" — capture time, device, GPS, software |
| `ela` | Write the amplified error-level map so a human can look at it |
| `localize` | The one pixel localization that measures: a cloned block, plus the ELA map |
| `compare` | Two versions of the same file — **a diff is exact, use it** |
| `timeline` | Order several files by capture time |

## Reading the result

The tool returns weighted signals, a band, and its own limits. Bands and what they
license you to say:

| Band | Say this | Do not say |
|---|---|---|
| `no_visible_tampering` | "Nothing this analysis can see points at editing." | "It's real", "it's genuine" |
| `weak_signals` | "Some weak indicators fired: …" | "It's fake" |
| `signs_of_editing` | "Several indicators point at editing: …" | "It's definitely forged" |
| `strong_signs_of_editing` | "Multiple independent signals point at editing; I would not accept this without the issuer's record." | "Proven forgery" |
| `contradicts_issuer_record` | "This document disagrees with the record you gave me — the amounts/dates don't line up." | Anything about intent |
| `credential_verified` | "The file carries a valid signed C2PA credential and no edit signal fired." | "Genuine beyond doubt" |

Always relay the **limits** the tool returns, at least the operative one. Always say
out loud which band you got and that it is evidence, not proof.

## What actually works, and what does not

**Every number below was produced by `python -m nanobot.forensics.benchmark` over a
rendered corpus — 20 clean receipts and 180 forgeries, verified on three seeds
(7, 11, 23). Do not replace them with adjectives.** Re-run the harness rather than
guessing if you change a gate.

### False positives, first

Across all three seeds, **0 of 60 clean receipts** were put outside the clean band.
That is the property to protect: one wrong accusation on a genuine receipt costs
more than a miss. If you touch a threshold, check this number first.

### The document layer, by forgery class

| class | files | total read by OCR | page fails to add up | verdict leaves clean band |
|---|---|---|---|---|
| clean | 12 | 12 | 0 (0%) | 0 (0%) |
| `replaced_amount` | 12 | 12 | 12 (**100%**) | 12 (100%) |
| `replaced_amount_same_quality` | 12 | 12 | 12 (**100%**) | 12 (100%) |
| `replaced_amount_low_quality` | 12 | 12 | 12 (**100%**) | 12 (100%) |

**This is the headline result of the whole feature.** A forged total is caught by
reconciliation — `subtotal + tax ≠ total` — at 100% with no false positives, at every
compression quality tested, including the case where the patch was saved at the same
quality as the page. That is the fraud class that matters on a receipt, and it is the
one class the pixel layer provably cannot see.

### The pixel layer, by forgery class

| forgery | caught | by |
|---|---|---|
| `cloned_block` (copy-stamp) | **20 / 20, all three seeds** | `block_grid` |
| `replaced_amount`, `_same_quality`, `_low_quality` | 0 / 60 | nothing |
| `patched_foreign_jpeg`, `_lossless` | 0 / 40 | nothing |
| `resized_patch`, `shifted_line`, `patched_from_other` | 0 / 60 | nothing |

Per-scan separation, seed 7, 200 files:

| scan | AUC | clean p50 / max | forged p50 / max |
|---|---|---|---|
| `block_grid` | 0.71 | 24.0 / 30.0 | 24.0 / 36.0 |
| `ela_max` | 0.69 | 11.0 / 22.0 | 13.0 / 19.0 |
| `sharpness` | 0.61 | 31.1 / 42.0 | 32.2 / 54.0 |
| `ghost` | 0.55 | 0.0 / 30.0 | 0.0 / 36.0 |
| `resample` | 0.54 | 0.0 / 0.0 | 0.0 / 4.0 |
| `noise` (wavelet) | 0.50 | 0.0 / 0.0 | 0.0 / 0.0 |

Read that honestly: **only a clone stamp is visible to pixels**, because a copied
region carries its own 8×8 block grid, which does not line up with the page's even
after a re-save. Everything else is re-encoded from scratch by the editing app and
leaves the pixel statistics the same as an untouched receipt. A scan whose AUC is at
or below 0.55 is reported in the result and deliberately not scored — `noise` is a
constant, and `resample`'s clean maximum moved from 0.0 to 0.752 between corpora,
which is a fitted number rather than a measured one.

**So a clean verdict on a re-encoded forgery is not a clearance.** The pixel layer
says nothing about the class you are most likely looking at.

### Demoted on measurement, and why

These ran, were measured, and were removed from the score. They are still printed,
and they must never be quoted at a user as evidence:

| check | why it was demoted |
|---|---|
| `line_spacing` | fired on 5/12 clean **and** 5/12 of the shifted-line forgeries. A rendered receipt has a blank separator before its reference block, which reads as one irregular baseline gap on an untouched page. It was the largest source of false positives in the pipeline. |
| `font_geometry` | 2/12 clean, 0/12 of the forgeries it was checked against. |
| `non_standard_quantisation_table` | fired on 100% of clean receipts: it compares against an IJG table that modern encoders do not emit. |
| `repeated_content_blocks` | legacy and DCT block-hash duplication both scored at chance. The gradient-hash count reached 230 on a *clean* page against a forged 90th percentile of 220. Repeated digits in a table look exactly like a clone. |

### What to rely on, in order

1. **Reconciliation against the issuer's record** (`expected_amount`, `expected_date`,
   `expected_reference`) — decisive when it mismatches; a match is *not* proof.
2. **`subtotal + tax ≠ total`** — 100% on the class measured, 0% false positives.
3. **A valid C2PA manifest** (`pip install c2pa`) — the only trustworthy positive
   signal. Its absence proves nothing.
4. **`compare` on two files** — an exact diff, and the right answer when a second
   version exists.
5. **`cloned_block` localization** — the one thing pixels can establish here.
6. **Error-level and noise maps** — a *picture for a human to look at*, never a
   score. On a text-dense receipt the tile-mean error peaks at 1.43 clean versus 1.61
   with a patch spliced in: text edges carry more error than the paste does.

A generated receipt has no editing history to find at all. Layout and arithmetic
checks are what catch that class, and they are heuristics, not proofs.

## Timestamps

`action="timestamps"` answers "when was this taken" from EXIF (`DateTimeOriginal`
first), then XMP, then nothing. Be blunt about the weak cases:

- No EXIF is **normal** for screenshots, chat-app re-saves and most receipt exports.
  Say the file cannot tell you when it was taken — do not fall back to the
  filesystem mtime as if it were the capture time. Any copy, download or upload
  rewrites it.
- Timestamps are written by whoever saved the file and can be set to anything.
  Treat a date as evidence only when it comes from the issuer's record or a signed
  credential.
- Report GPS when it is there, and say it is absent when it is not.

## Answering the user

Lead with the band and the single most important finding, then the limits, then what
would settle it. Suggested shape:

> No sign of editing, and the arithmetic is internally consistent (total 275,000 =
> 250,000 + 25,000). What I can't tell you from the file: there's no EXIF timestamp,
> so I can't say when it was taken, and no signed credential. To actually settle it,
> send me the amount from your bank statement or the merchant's record and I'll check
> the document against it.

When the user has the issuer's record, run `analyze` again with `expected_amount` —
do not re-run the whole analysis by hand.

## Escalating beyond this tool

If the stakes are high (insurance, legal, a large payment) say plainly that this is a
triage tool and the decision needs the issuer's record and, where it matters, a
qualified examiner. The repo-level research on open-source tamper detectors
(DocTamper, FakeShield, HiFi-IFDL, FatFormer, AIDE) is written up separately; none of
them is a drop-in for this and the strong ones need a GPU, so they are not wired in
by default.
