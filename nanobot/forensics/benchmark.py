"""A measured harness for the tamper detectors: synthetic receipts with known truth.

Every threshold in :mod:`nanobot.forensics.tamper` and every weight in
:mod:`nanobot.forensics.verdict` was chosen by running this harness, not by
taste. It renders receipts with Pillow, forges a known subset of them in the
ways a real fake receipt is produced, and reports how well each detector
separates the two classes.

Why a synthetic corpus: there is no public dataset of real fake receipts with
per-region ground truth, and there could not be one that stays valid — the
interesting forgeries are the ones nobody has published. A synthetic corpus is
honest about what it can and cannot prove. It cannot tell you how the detectors
behave on a photograph of a crumpled thermal receipt under mixed lighting; it can
tell you how they behave on the compression-history and layout manipulations that
make up most receipt fraud, and it catches the failure that matters most — a
detector that fires on *every* clean page.

Run it::

    python -m nanobot.forensics.benchmark            # summary table
    python -m nanobot.forensics.benchmark --json     # machine-readable

The printed ``separates`` column is the area under the ROC curve between the
clean and forged classes for that detector's score. 0.5 means the detector is
coin-flipping and its weight must be zero.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Font candidates, in order. The harness needs a real TrueType face: a bitmap
#: fallback font has no anti-aliasing and no glyph metrics, which would make the
#: layout detectors look better than they are.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/System/Library/Fonts/Menlo.ttc",
)

_MERCHANTS = (
    "NORTHGATE PHARMACY",
    "KOPI TUBAN CAFE",
    "HARBOUR POINT MARKET",
    "SUNRISE HARDWARE LTD",
    "TRATTORIA DEL PONTE",
    "BRIGHT MART EXPRESS",
)
_CURRENCIES = {"USD": "$", "IDR": "Rp ", "NGN": "NGN ", "EUR": "EUR "}


def _font_path() -> str | None:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


@dataclass
class Sample:
    """One corpus item: a file on disk plus the truth about it."""

    path: Path
    label: str  # "clean" or "forged"
    forgery: str = ""
    amount: float = 0.0
    reference: str = ""


@dataclass
class Receipt:
    """A rendered receipt and the ground-truth fields used to forge it."""

    image: object = None
    amount: float = 0.0
    tax: float = 0.0
    subtotal: float = 0.0
    reference: str = ""
    date: str = ""
    merchant: str = ""
    currency: str = "USD"
    amount_box: tuple[int, int, int, int] = (0, 0, 0, 0)
    date_box: tuple[int, int, int, int] = (0, 0, 0, 0)
    font_size: int = 20
    background: tuple[int, int, int] = (252, 251, 247)
    ink: tuple[int, int, int] = (28, 28, 30)
    lines: list[str] = field(default_factory=list)


def _money(value: float, currency: str) -> str:
    symbol = _CURRENCIES.get(currency, "$")
    if currency in ("IDR",):
        return f"{symbol}{value:,.0f}".replace(",", ".")
    return f"{symbol}{value:,.2f}"


def render_receipt(seed: int = 0, *, size: tuple[int, int] = (760, 1080)) -> Receipt:
    """Render one receipt with a realistic layout and record its true fields."""
    from PIL import Image, ImageDraw, ImageFont

    rng = random.Random(seed)
    path = _font_path()
    if path is None:  # pragma: no cover - only on a box with no TrueType font
        raise RuntimeError("no TrueType font available for the benchmark corpus")

    currency = rng.choice(list(_CURRENCIES))
    merchant = rng.choice(_MERCHANTS)
    font_size = rng.choice((18, 20, 22, 24))
    width, height = size
    background = (252, 251, 247) if rng.random() < 0.6 else (255, 255, 255)
    ink = (28, 28, 30) if rng.random() < 0.7 else (0, 0, 0)

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    body = ImageFont.truetype(path, font_size)
    bold = ImageFont.truetype(path, font_size)
    small = ImageFont.truetype(path, max(12, font_size - 4))

    item_count = rng.randint(3, 6)
    items: list[tuple[str, float]] = []
    for index in range(item_count):
        items.append((f"ITEM {index + 1:03d}", round(rng.uniform(1.5, 42.0), 2)))
    subtotal = round(sum(price for _, price in items), 2)
    tax = round(subtotal * rng.choice((0.0, 0.05, 0.1, 0.11, 0.2)), 2)
    amount = round(subtotal + tax, 2)
    if currency == "IDR":
        subtotal, tax, amount = round(subtotal * 1000), round(tax * 1000), round(amount * 1000)
        items = [(name, round(price * 1000)) for name, price in items]
    reference = f"TRX{rng.randint(10**9, 10**10 - 1)}"
    date = (
        f"{rng.randint(2024, 2026)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d} "
        f"{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}"
    )

    margin = 48
    y = 56
    draw.text((margin, y), merchant, font=bold, fill=ink)
    y += font_size + 18
    draw.text((margin, y), f"DATE {date}", font=body, fill=ink)
    y += font_size + 10
    draw.text((margin, y), f"REF  {reference}", font=body, fill=ink)
    y += font_size + 26
    for name, price in items:
        draw.text((margin, y), name, font=body, fill=ink)
        label = _money(price, currency)
        draw.text((width - margin - draw.textlength(label, font=body), y), label, font=body, fill=ink)
        y += font_size + 8
    y += 16
    for label, value in (("SUBTOTAL", subtotal), ("TAX", tax)):
        draw.text((margin, y), label, font=body, fill=ink)
        text = _money(value, currency)
        draw.text((width - margin - draw.textlength(text, font=body), y), text, font=body, fill=ink)
        y += font_size + 8
    y += 12
    draw.text((margin, y), "TOTAL", font=bold, fill=ink)
    total_text = _money(amount, currency)
    total_x = int(width - margin - draw.textlength(total_text, font=bold))
    draw.text((total_x, y), total_text, font=bold, fill=ink)
    amount_box = (
        total_x - 6,
        y - 4,
        int(draw.textlength(total_text, font=bold)) + 12,
        font_size + 10,
    )
    y += font_size + 26
    draw.text((margin, y), "THANK YOU", font=small, fill=ink)
    y += 30
    draw.text((margin, y), f"CARD ****{rng.randint(1000, 9999)}", font=small, fill=ink)

    return Receipt(
        image=image,
        amount=float(amount),
        tax=float(tax),
        subtotal=float(subtotal),
        reference=reference,
        date=date.split(" ")[0],
        merchant=merchant,
        currency=currency,
        amount_box=amount_box,
        date_box=(margin, 56 + font_size + 18, 320, font_size + 10),
        font_size=font_size,
        background=background,
        ink=ink,
        lines=[name for name, _ in items],
    )


def _save(image, path: Path, quality: int, *, noise: float = 0.0) -> None:
    """Save as JPEG, optionally adding sensor-like noise first."""
    from PIL import Image, ImageFilter

    out = image
    if noise > 0:
        arr = np.asarray(out.convert("RGB"), dtype=np.float32)
        rng = np.random.default_rng(int(noise * 100000) % (2**31))
        arr = arr + rng.normal(0.0, noise, arr.shape)
        out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    out = out.filter(ImageFilter.GaussianBlur(0.4))
    out.convert("RGB").save(path, format="JPEG", quality=quality)


def _paste_amount(receipt: Receipt, new_amount: float, quality: int, path: Path) -> None:
    """Forge the total the way it is actually done: patch it, then re-save.

    The patch is drawn from a *second* receipt rendered at a different JPEG
    quality when ``quality`` differs, which is what puts a different compression
    history inside the page. Patching and re-saving at the same quality is the
    easy case; the harness exercises both.
    """
    from PIL import Image, ImageDraw, ImageFont

    image = receipt.image.copy()
    draw = ImageDraw.Draw(image)
    x, y, w, h = receipt.amount_box
    draw.rectangle((x - 2, y - 2, x + w + 2, y + h + 2), fill=receipt.background)
    font = ImageFont.truetype(_font_path(), receipt.font_size)
    text = _money(new_amount, receipt.currency)
    draw.text((x, y), text, font=font, fill=receipt.ink)
    _save(image, path, quality)


def _paste_from_other(receipt: Receipt, other: Receipt, path: Path) -> None:
    """Copy a textured block from a second, differently-compressed receipt."""
    from PIL import Image

    source = other.image
    box = other.amount_box
    patch = source.crop(
        (max(0, box[0] - 10), max(0, box[1] - 8), box[0] + box[2] + 10, box[1] + box[3] + 8)
    )
    image = receipt.image.copy()
    target = receipt.amount_box
    image.paste(patch, (target[0] - 10, target[1] - 8))
    _save(image, path, 88)


def _patch_compressed_separately(receipt: Receipt, new_amount: float, quality: int, path: Path) -> None:
    """Build the patch as its own JPEG, then paste it into the page.

    This is the case that leaves a *second* compression grid and history inside
    the file: the patch was encoded on its own, at its own quality, so its 8x8
    blocks do not line up with the page's. Whether that survives the final save
    depends on how the final save is made, which is why the harness tries the
    final save at several qualities and at lossless.
    """
    from PIL import Image, ImageDraw, ImageFont

    x, y, w, h = receipt.amount_box
    patch = Image.new("RGB", (w + 12, h + 10), receipt.background)
    draw = ImageDraw.Draw(patch)
    font = ImageFont.truetype(_font_path(), receipt.font_size)
    draw.text((6, 5), _money(new_amount, receipt.currency), font=font, fill=receipt.ink)
    buffer = io.BytesIO()
    patch.save(buffer, format="JPEG", quality=quality, subsampling=2)
    buffer.seek(0)
    with Image.open(buffer) as encoded:
        encoded.load()
        pasted = encoded.convert("RGB")
    image = receipt.image.copy()
    image.paste(pasted, (max(0, x - 6), max(0, y - 5)))
    return image


def _paste_separately_compressed(receipt: Receipt, new_amount: float, quality: int,
                                 final: int, path: Path) -> None:
    image = _patch_compressed_separately(receipt, new_amount, quality, path)
    _save(image, path, final)


def _paste_into_png(receipt: Receipt, new_amount: float, path: Path) -> None:
    """A screenshot case: a JPEG fragment pasted onto a lossless page.

    Payment-app screenshots are PNG, so a number lifted from a JPEG and pasted
    in brings 8-periodic block noise into an image that has none. That is the one
    compression signature a final lossless save cannot erase.
    """
    image = _patch_compressed_separately(receipt, new_amount, 40, path)
    image.save(path, format="PNG", optimize=False)


def _resized_patch(receipt: Receipt, new_amount: float, path: Path) -> None:
    """Paste a patch that was scaled — the classic 'make the digits bigger' edit."""
    from PIL import Image, ImageDraw, ImageFont

    x, y, w, h = receipt.amount_box
    patch = Image.new("RGB", (w + 12, h + 10), receipt.background)
    draw = ImageDraw.Draw(patch)
    font = ImageFont.truetype(_font_path(), receipt.font_size)
    draw.text((6, 5), _money(new_amount, receipt.currency), font=font, fill=receipt.ink)
    scaled = patch.resize(
        (int(patch.width * 1.35), int(patch.height * 1.35)), Image.Resampling.BICUBIC
    )
    image = receipt.image.copy()
    image.paste(scaled, (max(0, x - 14), max(0, y - 8)))
    _save(image, path, 85)


def _clone_stamp(receipt: Receipt, path: Path) -> None:
    """Duplicate a textured block from elsewhere on the same page."""
    from PIL import Image

    image = receipt.image.copy()
    width, _ = image.size
    block = image.crop((60, 150, 60 + 150, 150 + 46))
    image.paste(block, (width // 2, 700))
    _save(image, path, 80)


def _shift_line_spacing(receipt: Receipt, path: Path) -> None:
    """Re-draw the total line a few pixels lower, as a text patcher would."""
    from PIL import Image, ImageDraw, ImageFont

    image = receipt.image.copy()
    draw = ImageDraw.Draw(image)
    x, y, w, h = receipt.amount_box
    draw.rectangle((x - 4, y - 4, x + w + 4, y + h + 14), fill=receipt.background)
    font = ImageFont.truetype(_font_path(), receipt.font_size)
    text = _money(receipt.amount, receipt.currency)
    draw.text((x, y + 11), text, font=font, fill=receipt.ink)
    _save(image, path, 85)


def build_corpus(root: Path, *, count: int = 40, seed: int = 7) -> list[Sample]:
    """Write a labelled corpus to ``root`` and return its manifest."""
    from PIL import ImageFilter

    root.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    rng = random.Random(seed)

    for index in range(count):
        receipt = render_receipt(seed * 1000 + index)
        clean_quality = rng.choice((45, 55, 65, 75, 88, 95))
        noise = rng.choice((0.0, 0.0, 1.5, 3.0))

        clean_path = root / f"clean-{index:03d}.jpg"
        candidate = receipt.image
        if rng.random() < 0.25:  # a phone photo of a screen: softened, then encoded
            candidate = candidate.filter(ImageFilter.GaussianBlur(1.1))
        _save(candidate, clean_path, clean_quality, noise=noise)
        samples.append(
            Sample(
                path=clean_path,
                label="clean",
                amount=receipt.amount,
                reference=receipt.reference,
            )
        )

        forged_amount = round(receipt.amount * rng.choice((2.0, 3.0, 5.0, 1.7)), 2)
        variants: list[tuple[str, object]] = [
            ("replaced_amount", lambda p, r=receipt, a=forged_amount: _paste_amount(r, a, 88, p)),
            ("replaced_amount_same_quality", lambda p, r=receipt, a=forged_amount: _paste_amount(r, a, clean_quality, p)),
            ("replaced_amount_low_quality", lambda p, r=receipt, a=forged_amount: _paste_amount(r, a, 45, p)),
            (
                "patched_foreign_jpeg",
                lambda p, r=receipt, a=forged_amount: _paste_separately_compressed(r, a, 35, 85, p),
            ),
            (
                "patched_foreign_jpeg_lossless",
                lambda p, r=receipt, a=forged_amount: _paste_into_png(r, a, p),
            ),
            ("resized_patch", lambda p, r=receipt, a=forged_amount: _resized_patch(r, a, p)),
            ("cloned_block", lambda p, r=receipt: _clone_stamp(r, p)),
            ("shifted_line", lambda p, r=receipt: _shift_line_spacing(r, p)),
            (
                "patched_from_other",
                lambda p, r=receipt, i=index: _paste_from_other(
                    r, render_receipt(seed * 1000 + ((i + 7) % count)), p
                ),
            ),
        ]
        for name, build in variants:
            target = root / f"forged-{name}-{index:03d}.jpg"
            build(target)  # type: ignore[operator]
            samples.append(
                Sample(
                    path=target,
                    label="forged",
                    forgery=name,
                    amount=receipt.amount,
                    reference=receipt.reference,
                )
            )
    return samples


def _auc(clean: list[float], forged: list[float]) -> float:
    """Area under the ROC curve, by rank comparison (ties count a half)."""
    if not clean or not forged:
        return float("nan")
    wins = 0.0
    for value in forged:
        for other in clean:
            if value > other:
                wins += 1.0
            elif value == other:
                wins += 0.5
    return wins / (len(clean) * len(forged))


def _detector_scores(forensics: dict) -> dict[str, float]:
    """One scalar per detector, higher meaning more suspicious."""
    grid = forensics.get("block_grid") or {}
    ghost = forensics.get("jpeg_ghost") or {}
    resample = forensics.get("resample") or {}
    noise = forensics.get("wavelet_noise") or {}
    copy_move = forensics.get("copy_move_blocks") or forensics.get("copy_move") or {}
    sharp = forensics.get("sharpness") or {}
    return {
        "block_grid_prom": float(grid.get("prominence") or 0.0),
        "sharpness_prom": float(sharp.get("prominence") or 0.0),
        "resample_prom": float(resample.get("prominence") or 0.0),
        "ghost_prom": float(ghost.get("prominence") or 0.0),
        "noise_prom": float(noise.get("prominence") or 0.0),
        "block_grid_tiles": float(grid.get("flagged_tiles") or 0.0),
        "sharpness_tiles": float(sharp.get("flagged_tiles") or 0.0),
        "copy_move": float(copy_move.get("duplicate_pairs") or 0.0),
        "ela_max": float((forensics.get("ela") or {}).get("max_abs_diff") or 0.0),
        "regions": float(len(forensics.get("regions") or [])),
    }


def evaluate(
    samples: list[Sample],
    *,
    keep_artifacts: bool = False,
    with_documents: bool = False,
    doc_per_class: int = 12,
) -> dict[str, object]:
    """Run ``analyse_image`` over the corpus and score every detector.

    With ``with_documents`` the OCR and reconciliation layer is measured over the
    amount-replaced classes as well, because that layer is the only one that can
    see a replaced total and the pixel table alone would understate the tool.
    """
    from nanobot.forensics.image_forensics import analyse_image

    per_label: dict[str, dict[str, list[float]]] = {"clean": {}, "forged": {}}
    failures: list[str] = []
    # One analysis per file, reused by all three tables below. Three passes gave
    # identical numbers and tripled the runtime of every calibration run.
    analyses: dict[str, dict] = {}
    for sample in samples:
        try:
            analyses[str(sample.path)] = analyse_image(
                sample.path, with_provenance=False
            ).as_dict()
        except Exception as exc:  # a detector that crashes is a detector that failed
            failures.append(f"{sample.path.name}: {type(exc).__name__}: {exc}")
    for sample in samples:
        result = analyses.get(str(sample.path))
        if result is None:
            continue
        for name, value in _detector_scores(result).items():
            per_label[sample.label].setdefault(name, []).append(value)

    rows: list[dict[str, object]] = []
    names = sorted(set(per_label["clean"]) | set(per_label["forged"]))
    for name in names:
        clean = per_label["clean"].get(name, [])
        forged = per_label["forged"].get(name, [])
        # The gate a detector needs to sit behind is a *quantile of the clean
        # class*, not the midpoint between the classes: one false accusation on a
        # genuine receipt costs far more than a miss, so the quantile is what
        # gets set and the detection rate is whatever that leaves.
        gate = (
            float(max(np.percentile(clean, 99.0), np.max(clean)))
            if clean else 0.0
        )
        rows.append(
            {
                "detector": name,
                "auc": round(_auc(clean, forged), 4),
                "clean_p50": round(float(np.median(clean)), 4) if clean else None,
                "clean_p95": round(float(np.percentile(clean, 95)), 4) if clean else None,
                "clean_p99": round(float(np.percentile(clean, 99)), 4) if clean else None,
                "clean_max": round(float(np.max(clean)), 4) if clean else None,
                "forged_p50": round(float(np.median(forged)), 4) if forged else None,
                "forged_p95": round(float(np.percentile(forged, 95)), 4) if forged else None,
                "gate_at_clean_max": round(float(gate), 4),
                "detect_at_gate": round(
                    float(np.mean(np.asarray(forged) > gate)) if forged else 0.0, 4
                ),
            }
        )
    rows.sort(key=lambda row: -(row["auc"] or 0.0))

    # Per-forgery breakdown: which manipulations the current verdict catches.
    from nanobot.forensics.verdict import score

    by_forgery: dict[str, dict[str, float]] = {}
    for sample in samples:
        if sample.label != "forged":
            continue
        result = analyses.get(str(sample.path))
        if result is None:
            continue
        verdict = score(result, None)
        entry = by_forgery.setdefault(sample.forgery, {"n": 0.0, "flagged": 0.0})
        entry["n"] += 1
        if verdict["band"] != "no_visible_tampering":
            entry["flagged"] += 1
    for name, entry in by_forgery.items():
        entry["rate"] = round(entry["flagged"] / max(1.0, entry["n"]), 3)

    flagged_clean = 0
    clean_total = 0
    # A false positive is only actionable if the report says which signal caused
    # it. Keeping the names here is what turned "7 of 20 clean files were
    # flagged" into one line of code away from the fix.
    clean_reasons: list[dict[str, object]] = []
    for sample in samples:
        if sample.label != "clean":
            continue
        clean_total += 1
        result = analyses.get(str(sample.path))
        if result is None:
            continue
        verdict = score(result, None)
        if verdict["band"] != "no_visible_tampering":
            flagged_clean += 1
            clean_reasons.append(
                {
                    "file": sample.path.name,
                    "band": verdict["band"],
                    "score": verdict.get("score"),
                    "signals": [
                        s.get("signal") for s in verdict.get("signals", [])
                        if s.get("direction") == "edit_signal"
                    ],
                }
            )

    documents: dict[str, object] = {"available": False, "reason": "not requested"}
    if with_documents:
        # Reuse the analyses already computed above rather than re-reading every
        # file: the OCR pass is the slow part and the pixels do not change.
        _IMAGE_CACHE.update(analyses)
        documents = evaluate_documents(samples, per_class=doc_per_class)

    return {
        "detectors": rows,
        "by_forgery": by_forgery,
        "documents": documents,
        "clean_flagged": flagged_clean,
        "clean_total": clean_total,
        "clean_false_positive_rate": round(flagged_clean / max(1, clean_total), 4),
        "clean_false_positives": clean_reasons,
        "failures": failures,
        "keep_artifacts": keep_artifacts,
    }


#: The classes where the amount printed on the page was changed. These are the
#: forgeries the pixel layer provably cannot see, so the document layer is the
#: only thing standing between them and a clean report.
AMOUNT_FORGERIES = (
    "replaced_amount",
    "replaced_amount_same_quality",
    "replaced_amount_low_quality",
)


def evaluate_documents(
    samples: list[Sample], *, per_class: int = 12, root: Path | None = None
) -> dict[str, object]:
    """Run OCR + reconciliation over the clean and amount-replaced classes.

    Reports, per class, how often the total was read at all, how often the page
    failed to add up, and how often the *whole verdict* (pixels plus document)
    left the clean band. The first number is the ceiling on the other two: if the
    OCR cannot read the total, no arithmetic check can help, and saying so is
    more useful than a percentage that hides it.
    """
    from nanobot.forensics.document_forensics import analyse_document, ocr_available
    from nanobot.forensics.verdict import score

    if not ocr_available():
        return {"available": False, "reason": "tesseract is not installed"}

    wanted: dict[str, list[Sample]] = {"clean": []}
    for name in AMOUNT_FORGERIES:
        wanted[name] = []
    for sample in samples:
        key = "clean" if sample.label == "clean" else sample.forgery
        if key in wanted and len(wanted[key]) < per_class:
            wanted[key].append(sample)

    out: dict[str, object] = {"available": True, "classes": {}}
    classes: dict[str, dict[str, object]] = {}
    for key, items in wanted.items():
        if not items:
            continue
        read = inconsistent = flagged = 0
        failures: list[str] = []
        for sample in items:
            try:
                document = analyse_document(sample.path)
                images = analyse_image_cached(sample.path)
            except Exception as exc:  # a crash is a failed measurement, not a stop
                failures.append(f"{sample.path.name}: {type(exc).__name__}: {exc}")
                continue
            arithmetic = (document.get("arithmetic") or {})
            if arithmetic.get("total") is not None:
                read += 1
            if arithmetic.get("consistent") is False:
                inconsistent += 1
            if score(images, document)["band"] != "no_visible_tampering":
                flagged += 1
        n = max(1, len(items) - len(failures))
        classes[key] = {
            "n": len(items),
            "total_read": read,
            "arithmetic_failed": inconsistent,
            "verdict_flagged": flagged,
            "arithmetic_failed_rate": round(inconsistent / n, 3),
            "verdict_flagged_rate": round(flagged / n, 3),
            "failures": failures,
        }
    out["classes"] = classes
    return out


_IMAGE_CACHE: dict[str, dict] = {}


def analyse_image_cached(path: Path) -> dict:
    """``analyse_image`` with a process-local cache, so a corpus is read once."""
    from nanobot.forensics.image_forensics import analyse_image

    key = str(path)
    if key not in _IMAGE_CACHE:
        _IMAGE_CACHE[key] = analyse_image(path, with_provenance=False).as_dict()
    return _IMAGE_CACHE[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the tamper detectors.")
    parser.add_argument("--count", type=int, default=24, help="clean receipts to render")
    parser.add_argument("--root", default="/tmp/forensics-corpus")
    parser.add_argument(
        "--seed", type=int, default=7,
        help="corpus seed. Calibrate on one seed and verify on another — a gate fitted "
             "and checked on the same files is a fitted number, not a measured one.",
    )
    parser.add_argument(
        "--documents", action="store_true",
        help="also measure the OCR + reconciliation layer over the amount-replaced "
             "classes, which is where a forged total is actually caught",
    )
    parser.add_argument(
        "--doc-rows", type=int, default=12,
        help="files per class to OCR when --documents is given",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if _font_path() is None:  # pragma: no cover
        print("No TrueType font found; the benchmark cannot render its corpus.", file=sys.stderr)
        return 2

    samples = build_corpus(Path(args.root), count=args.count, seed=args.seed)
    report = evaluate(samples, with_documents=args.documents, doc_per_class=args.doc_rows)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"corpus: {len(samples)} files under {args.root}")
    print(f"{'detector':24} {'auc':>7} {'clean p95':>10} {'forged p95':>11}")
    for row in report["detectors"]:  # type: ignore[union-attr]
        print(
            f"{row['detector']:24} {row['auc']:>7} {str(row['clean_p95']):>10} "
            f"{str(row['forged_p95']):>11}"
        )
    print()
    print(f"clean pages flagged by the current verdict: "
          f"{report['clean_flagged']}/{report['clean_total']} "
          f"({report['clean_false_positive_rate']})")
    for name, entry in sorted(report["by_forgery"].items()):  # type: ignore[union-attr]
        print(f"  {name:28} caught {entry['flagged']:.0f}/{entry['n']:.0f} ({entry['rate']})")
    documents = report.get("documents") or {}
    if args.documents:
        print()
        if not documents.get("available"):
            print(f"document layer not measured: {documents.get('reason')}")
        else:
            print("document layer (OCR + reconciliation):")
            print(f"  {'class':32} {'n':>3} {'read':>5} {'math fails':>11} {'flagged':>8}")
            for name, entry in sorted((documents.get("classes") or {}).items()):
                print(
                    f"  {name:32} {entry['n']:>3} {entry['total_read']:>5} "
                    f"{entry['arithmetic_failed']:>6} ({entry['arithmetic_failed_rate']}) "
                    f"{entry['verdict_flagged']:>4} ({entry['verdict_flagged_rate']})"
                )
            for name, entry in sorted((documents.get("classes") or {}).items()):
                for failure in entry.get("failures") or []:
                    print(f"  ! {name}: {failure}")
    if report["failures"]:
        print("\nfailures:")
        for failure in report["failures"]:
            print(f"  {failure}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
