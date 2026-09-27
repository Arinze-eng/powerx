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
| `localize` | Repeated-content blocks, plus the ELA map |
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

Verified by measurement in this repo, not assumed:

- **Reconciliation against the issuer's record** — the only check strong enough to
  act on. One mismatch is decisive; a match is *not* proof the image is unedited.
- **A valid C2PA manifest** (`pip install c2pa`) — signed provenance, the only
  trustworthy positive signal. Its absence proves nothing, because almost no camera
  or receipt app writes one yet.
- **Document geometry** (OCR word boxes) — font-height outliers, broken line
  spacing, a repeated reference number, and **total ≠ subtotal + tax**. This is what
  scales a "photoshop the amount" forgery, and it has no pixels to hide behind.
- **`compare` on two files** — exact, and the right answer when a second version
  exists.
- **Error-level analysis** — a *picture for a human to look at*, never a score. It
  was measured here: on a text-dense receipt the tile-mean error peaks at 1.43 clean
  versus 1.61 with a patch spliced in, so text edges carry more error than a paste
  does. Any threshold that fires on the paste also fires on the text.
- **Noise / sharpness maps** — the same story: they mostly measure how much of the
  page is blank versus text.
- **Everything above is destroyed by one re-encode**, a screenshot, or a
  print-and-scan round trip. Absence of signals proves nothing.

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
