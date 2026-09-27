"""Receipt- and document-specific checks: layout geometry, text lines, amounts.

This is the layer that catches the forgeries pixels cannot: a receipt whose line
spacing drifts, whose font changes size mid-document, whose total does not equal
its own subtotal plus tax, or whose reference number appears twice. All of it is
read off OCR word boxes plus the geometry of the image itself, so it runs offline.

The important limit, stated plainly: none of this can tell a real receipt issued
by a real terminal from a real-looking receipt rendered by a generator. Only the
issuer's own record can do that. :func:`reconcile` is the hook for exactly that,
and it is the check that actually settles the question.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

#: Amounts as they appear on receipts, with or without a currency symbol.
#: The number is captured loosely — every digit and separator in a run — and
#: :func:`_normalise_number` decides what the separators mean. A precise pattern
#: here was actively harmful: `\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?` backtracked
#: into reading "IDR 275.000" as 275.00, which silently broke every IDR receipt.
_AMOUNT = re.compile(
    r"(?:(?P<cur>[$€£₦₹]|RP|IDR|NGN|USD|EUR|GBP|MYR|PHP|THB|VND|KES|GHS|ZAR)\s*)?"
    r"(?P<num>\d[\d.,]*)",
)

#: Words that mark the line that must reconcile.
_TOTAL_WORDS = ("total", "jumlah", "grand total", "amount due", "total bayar", "balance")
_SUBTOTAL_WORDS = ("subtotal", "sub total", "dpp", "net")
_TAX_WORDS = ("tax", "vat", "ppn", "gst", "service charge")

#: A long digit run that should be unique on a receipt.
_REFERENCE = re.compile(r"\b(?:[A-Z]{1,4}[-/]?)?\d{8,}\b")

_INCONSISTENT_FONT_PENALTY = 2.6
_DUPLICATE_PENALTY = 1.8
_ARITHMETIC_PENALTY = 4.0
_DRIFT_PENALTY = 2.2

#: Findings that are reported but deliberately carry no weight, because the
#: harness measured them firing on untouched receipts at the same rate as on
#: edited ones. See the note in :func:`analyse_document` for the table.
_REPORT_ONLY_FINDINGS = ("font_geometry", "line_spacing")


@dataclass
class TextLine:
    """One OCR'd line with the geometry that makes a forgery visible."""

    text: str
    top: int
    height: int
    left: int
    right: int
    confidence: float
    words: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "top": self.top,
            "height": self.height,
            "left": self.left,
            "right": self.right,
            "confidence": round(self.confidence, 1),
        }


def ocr_available() -> bool:
    """True when the ``tesseract`` binary is on PATH."""
    return shutil.which("tesseract") is not None


def _tesseract_tsv(path: Path, psm: int = 6) -> list[dict[str, str]]:
    """Run tesseract and return its TSV rows (word boxes + confidence)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "out"
        cmd = [
            "tesseract",
            str(path),
            str(base),
            "--psm",
            str(psm),
            "-l",
            "eng",
            "tsv",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        tsv = base.with_suffix(".tsv")
        if proc.returncode != 0 or not tsv.exists():
            raise RuntimeError(
                (proc.stderr or proc.stdout or "tesseract produced no output").strip()[:300]
            )
        raw = tsv.read_text(errors="replace").splitlines()
    if not raw:
        return []
    header = raw[0].split("\t")
    rows = []
    for line in raw[1:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue
        rows.append(dict(zip(header, parts, strict=False)))
    return rows


def _rows_to_lines(rows: list[dict[str, str]]) -> list[TextLine]:
    # line_num restarts inside every paragraph, so (block, paragraph, line) is
    # the triple that actually identifies one physical line. Grouping on line_num
    # alone welds unrelated lines of the page into one — measured: a 12-line
    # receipt collapsed into 3 lines and the arithmetic checks then read nonsense.
    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        key = (
            row.get("page_num", "1"),
            row.get("block_num", "0"),
            row.get("par_num", "0"),
            row.get("line_num", "0"),
        )
        grouped.setdefault(key, []).append(row)

    lines: list[TextLine] = []
    for _, words in grouped.items():
        def _int(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        tops = [_int(w.get("top")) for w in words]
        heights = [max(1, _int(w.get("height"), 1)) for w in words]
        confidences = [
            float(w.get("conf")) for w in words if str(w.get("conf", "-1")).replace(".", "").isdigit()
        ]
        text = " ".join((w.get("text") or "").strip() for w in words).strip()
        if not text:
            continue
        lefts = [_int(w.get("left")) for w in words]
        rights = [_int(w.get("left")) + _int(w.get("width")) for w in words]
        lines.append(
            TextLine(
                text=text,
                top=int(np.median(tops)),
                height=int(np.median(heights)),
                left=min(lefts),
                right=max(rights),
                confidence=float(np.mean(confidences)) if confidences else 0.0,
                words=[
                    {
                        "text": (w.get("text") or "").strip(),
                        "left": _int(w.get("left")),
                        "top": _int(w.get("top")),
                        "width": _int(w.get("width")),
                        "height": _int(w.get("height")),
                        "conf": w.get("conf"),
                    }
                    for w in words
                ],
            )
        )
    lines.sort(key=lambda line: line.top)
    return lines


#: Dates and times masquerade as amounts ("27/09/2026" -> 2026.00). They are
#: removed before any money is read.
_DATE_LIKE = re.compile(
    r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}(?::\d{2})?"
)


def _parse_number(text: str) -> float | None:
    """Parse the largest money-looking number in a string.

    A bare run of digits is not money: a reference number is not a total, and a
    four-digit year is not a total either. A number only counts when it carries a
    currency marker or a thousands/decimal separator.
    """
    cleaned = _DATE_LIKE.sub(" ", text)
    best: float | None = None
    for match in _AMOUNT.finditer(cleaned):
        raw = match.group("num")
        marker = match.group("cur")
        if not marker and not any(sep in raw for sep in ".,"):
            continue
        try:
            value = float(_normalise_number(raw))
        except ValueError:
            continue
        if best is None or value > best:
            best = value
    return best


def _normalise_number(raw: str) -> str:
    """Resolve ``.`` and ``,`` into a float string.

    Receipts come in both conventions — ``IDR 275.000`` means 275000 and
    ``USD 275.00`` means 275 — so the separator is judged by what follows the
    *last* one: one or two digits means it is a decimal point, three means it is a
    thousands separator. Measured against the alternative (treat ``275.000`` as
    275.00) this is the reading that matches how receipts are actually printed.
    """
    sep_at = max(raw.rfind("."), raw.rfind(","))
    if sep_at < 0:
        return raw
    sep = raw[sep_at]
    head, tail = raw[:sep_at], raw[sep_at + 1:]
    if len(tail) in (1, 2):
        return f"{head.replace('.', '').replace(',', '')}.{tail}"
    return raw.replace(".", "").replace(",", "")


def _line_matches(text: str, needles: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(needle in low for needle in needles)


def _font_consistency(lines: list[TextLine]) -> dict[str, Any]:
    """Character height per line should sit in a small band on a genuine render."""
    heights = np.asarray([line.height for line in lines if len(line.text) >= 4], dtype=np.float32)
    if heights.size < 4:
        return {"available": False}
    median = float(np.median(heights))
    mad = float(np.median(np.abs(heights - median))) or 1.0
    z = (heights - median) / (1.4826 * mad)
    outliers = [
        {"text": line.text[:60], "height": int(line.height), "z": round(float(zz), 2)}
        for line, zz in zip(
            [line for line in lines if len(line.text) >= 4], z, strict=False
        )
        if abs(float(zz)) > 3.5
    ]
    return {
        "available": True,
        "median_height": median,
        "mad": round(mad, 2),
        "outliers": outliers[:8],
        "outlier_count": len(outliers),
    }


def _line_spacing(lines: list[TextLine]) -> dict[str, Any]:
    """Baseline gaps should be regular; a pasted line breaks the rhythm."""
    tops = np.asarray([line.top for line in lines], dtype=np.float32)
    if tops.size < 5:
        return {"available": False}
    gaps = np.diff(tops)
    typical = float(np.median(gaps))
    if typical <= 0:
        return {"available": False}
    irregular = [
        {
            "after_line": lines[index].text[:50],
            "gap": int(gaps[index]),
            "expected": int(typical),
        }
        for index in range(len(gaps))
        if abs(float(gaps[index]) - typical) > 0.55 * typical
    ]
    return {
        "available": True,
        "typical_gap": typical,
        "irregular": irregular[:8],
        "irregular_count": len(irregular),
    }


def _duplicate_identifiers(lines: list[TextLine]) -> list[dict[str, Any]]:
    """A reference number that appears twice is a paste, not a terminal print."""
    seen: dict[str, int] = {}
    dupes: list[dict[str, Any]] = []
    for line in lines:
        for match in _REFERENCE.finditer(line.text):
            token = match.group(0)
            if len(token) < 8:
                continue
            if token in seen:
                dupes.append({"token": token, "lines": [seen[token], line.text[:60]]})
            else:
                seen[token] = line.text[:60]
    return dupes[:8]


def _classify_amount_line(text: str) -> str | None:
    """Which of subtotal/tax/total this line is, if any.

    Order matters: "Subtotal" contains "total", and a naive substring test reads a
    subtotal line as the grand total, which silently corrupts the arithmetic
    check. Subtotal and tax are therefore claimed first, and a grand total must
    not also read as either.
    """
    low = text.lower()
    if _line_matches(low, _SUBTOTAL_WORDS):
        return "subtotal"
    if _line_matches(low, _TAX_WORDS):
        return "tax"
    if _line_matches(low, _TOTAL_WORDS):
        return "total"
    return None


def _arithmetic(lines: list[TextLine]) -> dict[str, Any]:
    """Does the printed total equal its own parts? Forgers often miss this."""
    total = subtotal = tax = None
    total_line = subtotal_line = tax_line = None
    for line in lines:
        kind = _classify_amount_line(line.text)
        if kind == "subtotal" and subtotal is None:
            subtotal, subtotal_line = _parse_number(line.text), line.text[:60]
        elif kind == "tax" and tax is None:
            tax, tax_line = _parse_number(line.text), line.text[:60]
        elif kind == "total" and total is None:
            value = _parse_number(line.text)
            if value is not None:
                total, total_line = value, line.text[:60]
    out: dict[str, Any] = {
        "available": False,
        "total": total,
        "subtotal": subtotal,
        "tax": tax,
        "total_line": total_line,
        "subtotal_line": subtotal_line,
        "tax_line": tax_line,
        "consistent": None,
        "difference": None,
    }
    if total is None:
        return out
    out["available"] = True
    if subtotal is not None and tax is not None:
        expected = subtotal + tax
        diff = abs(expected - total)
        out["expected_total"] = round(expected, 2)
        out["difference"] = round(diff, 2)
        out["consistent"] = diff <= max(0.02, 0.01 * max(1.0, total))
    elif subtotal is not None:
        out["difference"] = None
        out["consistent"] = None
        out["note"] = "A subtotal was read but no tax line, so the sum cannot be checked."
    return out


def _amount_region_consistency(path: Path, lines: list[TextLine]) -> dict[str, Any]:
    """Compare local compression/noise at the total line against the whole receipt.

    A number typed over the top of a rendered receipt sits at a different JPEG
    generation than the rest of the page, so its local error level stands out.
    """
    try:
        from PIL import Image

        from nanobot.forensics.image_forensics import ela_map, block_map
    except Exception:
        return {"available": False}

    target = None
    for line in lines:
        if _classify_amount_line(line.text) == "total":
            target = line
            break
    if target is None:
        return {"available": False, "reason": "no total line located"}

    try:
        with Image.open(path) as img:
            img.load()
            diff, _disp, _scale = ela_map(img)
            tiles = block_map(diff, block=16)
            h, w = diff.shape[:2]
            y0, y1 = max(0, target.top - 4), min(h, target.top + target.height + 4)
            x0, x1 = max(0, target.left - 8), min(w, target.right + 8)
            patch = diff[y0:y1, x0:x1]
            if patch.size < 64:
                return {"available": False, "reason": "total region too small"}
            local = float(np.percentile(patch, 95))
            frame = float(np.percentile(diff, 95))
            floor = float(np.median(tiles)) or 1.0
            return {
                "available": True,
                "region": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
                "local_p95_error": round(local, 2),
                "frame_p95_error": round(frame, 2),
                "ratio": round(local / failure_floor(frame), 3),
                "tile_median_error": round(floor, 2),
                "note": (
                    "The amount is normally the highest-contrast text on a receipt, so "
                    "some elevation is expected. Only a large ratio alongside other "
                    "signals is worth acting on."
                ),
            }
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def failure_floor(value: float) -> float:
    """Guard against divide-by-zero when a frame is perfectly flat."""
    return value if value > 1e-6 else 1e-6


def reconcile(
    analysis: dict[str, Any],
    expected_amount: float | str | None = None,
    expected_date: str | None = None,
    expected_reference: str | None = None,
) -> dict[str, Any]:
    """Compare what the document says against what the issuer says it should be.

    This is the only check that can actually settle whether a receipt is real: it
    does not care how the pixels look, it cares whether the merchant's or bank's
    own record matches. Everything else in this package is supporting evidence.
    """
    out: dict[str, Any] = {
        "asked": any(v is not None for v in (expected_amount, expected_date, expected_reference)),
        "checked": False,
        "mismatches": [],
        "matches": [],
        "note": "",
    }
    if not out["asked"]:
        out["note"] = (
            "Nothing to reconcile against. To actually settle authenticity, supply the "
            "amount (and date or reference) from the issuer's own record — a bank "
            "statement line, a payment gateway dashboard, a merchant till export."
        )
        return out

    lines = analysis.get("text_lines") or []
    joined = "\n".join(line.get("text", "") for line in lines)
    out["checked"] = True

    if expected_amount is not None:
        try:
            want = float(str(expected_amount).replace(",", "").strip())
        except ValueError:
            want = None
        if want is not None:
            total = (analysis.get("arithmetic") or {}).get("total")
            if total is None:
                out["mismatches"].append(
                    {"field": "amount", "expected": want, "found": None,
                     "detail": "no total could be read from the document"}
                )
            elif abs(total - want) <= 0.01:
                out["matches"].append({"field": "amount", "value": total})
            else:
                out["mismatches"].append(
                    {"field": "amount", "expected": want, "found": total,
                     "detail": f"document says {total}, record says {want}"}
                )

    if expected_date:
        wanted = str(expected_date).strip()
        compact = re.sub(r"\D", "", wanted)
        if compact and compact in re.sub(r"\D", "", joined):
            out["matches"].append({"field": "date", "value": wanted})
        else:
            out["mismatches"].append(
                {"field": "date", "expected": wanted, "found": None,
                 "detail": "that date does not appear in the document text"}
            )

    if expected_reference:
        wanted = str(expected_reference).strip()
        if wanted and wanted.lower() in joined.lower():
            out["matches"].append({"field": "reference", "value": wanted})
        else:
            out["mismatches"].append(
                {"field": "reference", "expected": wanted, "found": None,
                 "detail": "that reference does not appear in the document text"}
            )

    out["settled"] = bool(out["mismatches"])
    out["note"] = (
        "A mismatch here is the strongest result this package can produce: it means the "
        "document disagrees with the issuer's record. A match is weaker — it means the "
        "document is consistent with one transaction, not that the image is unedited."
    )
    return out


def analyse_document(
    path: Path,
    *,
    expected_amount: float | str | None = None,
    expected_date: str | None = None,
    expected_reference: str | None = None,
    max_lines: int = 400,
) -> dict[str, Any]:
    """Layout, text and arithmetic checks for a receipt or any text document."""
    out: dict[str, Any] = {
        "text_available": False,
        "ocr_engine": None,
        "text_lines": [],
        "fonts": {},
        "spacing": {},
        "duplicates": [],
        "arithmetic": {},
        "amount_region": {},
        "findings": [],
        "characters_read": 0,
    }

    lines: list[TextLine] = []
    if ocr_available():
        try:
            rows = _tesseract_tsv(path)
            lines = _rows_to_lines(rows)[:max_lines]
            out["ocr_engine"] = "tesseract"
            out["text_available"] = bool(lines)
        except Exception as exc:
            out["ocr_error"] = f"{type(exc).__name__}: {exc}"
    else:
        out["ocr_error"] = (
            "No tesseract binary found, so text-level checks (arithmetic, duplicate "
            "references, font consistency) could not run. Pixel checks still ran."
        )

    out["text_lines"] = [line.as_dict() for line in lines]
    out["characters_read"] = sum(len(line.text) for line in lines)

    if lines:
        out["fonts"] = _font_consistency(lines)
        out["spacing"] = _line_spacing(lines)
        out["duplicates"] = _duplicate_identifiers(lines)
        out["arithmetic"] = _arithmetic(lines)
        out["amount_region"] = _amount_region_consistency(path, lines)

        if out["fonts"].get("outlier_count"):
            out["findings"].append(
                {
                    "signal": "font_geometry",
                    "detail": (
                        f"{out['fonts']['outlier_count']} line(s) have a character height "
                        "far from the document's median: "
                        + "; ".join(o["text"] for o in out["fonts"]["outliers"][:3])
                    ),
                    "weight": _INCONSISTENT_FONT_PENALTY,
                }
            )
        if out["spacing"].get("irregular_count"):
            out["findings"].append(
                {
                    "signal": "line_spacing",
                    "detail": (
                        f"{out['spacing']['irregular_count']} baseline gap(s) break the "
                        f"document's own rhythm (typical {int(out['spacing']['typical_gap'])}px)"
                    ),
                    "weight": _DRIFT_PENALTY,
                }
            )
        if out["duplicates"]:
            out["findings"].append(
                {
                    "signal": "duplicate_reference",
                    "detail": "the same long number appears more than once: "
                    + ", ".join(d["token"] for d in out["duplicates"][:3]),
                    "weight": _DUPLICATE_PENALTY,
                }
            )
        if out["arithmetic"].get("consistent") is False:
            out["findings"].append(
                {
                    "signal": "arithmetic",
                    "detail": (
                        f"printed total {out['arithmetic'].get('total')} does not equal "
                        f"subtotal + tax ({out['arithmetic'].get('expected_total')}), "
                        f"off by {out['arithmetic'].get('difference')}"
                    ),
                    "weight": _ARITHMETIC_PENALTY,
                }
            )

    # Two of the four content checks are demoted to measurements, by measurement.
    #
    # ``line_spacing`` fires at the same rate on clean and forged files. The
    # reason is visible in the data: a rendered receipt has a blank separator
    # before its reference and date block, so an untouched page already has one
    # gap that does not match the document's rhythm — the same shape a spliced
    # line produces. Over 12 files per class it flagged 5/12 clean and 5/12
    # forged, which is a coin toss, and it was the single largest source of false
    # positives in the whole pipeline. ``font_geometry`` flagged 2/12 clean and
    # 0/12 of the forgeries it was checked against.
    #
    # ``arithmetic`` is kept and is the point of this layer: it flagged 0/12 clean
    # and 12/12 of all three amount-replacement classes, because replacing the
    # printed total without also rewriting the subtotal and tax leaves the page
    # unable to add up. The pixel layer cannot see that class at all.
    for finding in out["findings"]:
        if finding.get("signal") in _REPORT_ONLY_FINDINGS:
            finding["weight"] = 0.0
            finding["direction"] = "no_signal"
            finding["detail"] = (
                str(finding.get("detail") or "")
                + " — reported, not scored: this check fired on untouched receipts at "
                "the same rate as on edited ones, so it is not evidence either way."
            )

    out["reconciliation"] = reconcile(
        out,
        expected_amount=expected_amount,
        expected_date=expected_date,
        expected_reference=expected_reference,
    )
    return out


def crop_region(path: Path, region: dict[str, Any], out_path: Path) -> Path:
    """Cut a suspicious rectangle out of an image so it can be looked at closely."""
    from PIL import Image

    with Image.open(path) as img:
        img.load()
        box = (
            int(region.get("x", 0)),
            int(region.get("y", 0)),
            int(region.get("x", 0)) + int(region.get("width", 64)),
            int(region.get("y", 0)) + int(region.get("height", 64)),
        )
        img.crop(box).save(out_path)
    return out_path


def heatmap_overlay(path: Path, regions: list[dict[str, Any]], out_path: Path) -> Path:
    """Draw the flagged rectangles over the image so a human can judge them."""
    from PIL import Image, ImageDraw

    with Image.open(path) as img:
        base = img.convert("RGB")
        draw = ImageDraw.Draw(base)
        for index, region in enumerate(regions, start=1):
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            w = int(region.get("width", 48))
            h = int(region.get("height", 48))
            draw.rectangle([x, y, x + w, y + h], outline=(255, 64, 64), width=3)
            draw.text((x + 4, y + 4), f"{index}", fill=(255, 255, 0))
        base.save(out_path)
    return out_path


def ela_image(path: Path, out_path: Path, quality: int = 90, amplify: int = 12) -> Path:
    """Write the amplified error-level map as a viewable PNG."""
    from PIL import Image

    from nanobot.forensics.image_forensics import ela_map

    with Image.open(path) as img:
        img.load()
        _diff, disp, _scale = ela_map(img, quality=quality)
    Image.fromarray(np.clip(disp * amplify, 0, 255).astype(np.uint8)).save(out_path)
    return out_path
