"""Forensic analysis of receipts and documents: the checks that must and must not fire.

The load-bearing test here is test_clean_receipt_raises_nothing. If a clean receipt
can trip an edit signal, the tool is worse than useless on real receipts, because a
false "edited" is the failure that gets an honest claim refused. Every threshold in
the package was set against that constraint.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PIL.PngImagePlugin import PngInfo  # noqa: E402

from nanobot.agent.tools.media_forensics import MediaForensicsTool  # noqa: E402
from nanobot.forensics import analyse_document, analyse_image, score  # noqa: E402
from nanobot.forensics.document_forensics import _parse_number, ocr_available  # noqa: E402
from nanobot.forensics.image_forensics import duplicate_regions, provenance_check  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
needs_ocr = pytest.mark.skipif(not ocr_available(), reason="tesseract is not installed")
needs_font = pytest.mark.skipif(not Path(FONT).exists(), reason="test font not installed")


def _receipt_lines(total: str = "IDR 275.000") -> list[str]:
    return [
        "WONDR TRANSFER RECEIPT",
        "Bank Nasional Indonesia",
        "Date: 27/09/2026 14:03:11",
        "Reference: 20260927140311009876",
        "Sender: ARINZE O",
        "Recipient: PT SUMBER MAKMUR",
        "Account: 1234567890",
        "Subtotal: IDR 250.000",
        "Tax: IDR 25.000",
        f"Total: {total}",
        "Transfer successful",
    ]


def _draw(lines: list[str], size: tuple[int, int] = (760, 1000)) -> Image.Image:
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 22)
    y = 60
    for line in lines:
        draw.text((50, y), line, fill=(20, 20, 20), font=font)
        y += 46
    return img


def _exif(img: Image.Image, *, software: str = "", written: str = "2026:09:27 14:03:11"):
    exif = img.getexif()
    exif[271] = "samsung"
    exif[272] = "SM-S911B"
    exif[305] = software
    exif[306] = written
    exif[36867] = "2026:09:27 14:03:11"
    return exif


def _write_jpeg(path: Path, img: Image.Image, exif=None, quality: int = 92) -> Path:
    if exif is None:
        img.save(path, "JPEG", quality=quality)
    else:
        img.save(path, "JPEG", quality=quality, exif=exif)
    return path


@pytest.fixture
def clean_receipt(tmp_path: Path) -> Path:
    img = _draw(_receipt_lines())
    return _write_jpeg(tmp_path / "clean.jpg", img, _exif(img))


# -- the load-bearing test -----------------------------------------------------


def test_clean_receipt_raises_nothing(clean_receipt: Path):
    forensics = analyse_image(clean_receipt).as_dict()
    document = analyse_document(clean_receipt)
    verdict = score(forensics, document)

    assert verdict["band"] == "no_visible_tampering", verdict["signals"]
    assert verdict["positive_score"] == 0.0
    assert forensics["regions"] == []
    assert verdict["cannot_prove_genuine"] is True
    assert verdict["limits"]


@needs_font
@needs_ocr
def test_clean_receipt_arithmetic_is_consistent(clean_receipt: Path):
    document = analyse_document(clean_receipt)
    assert document["text_available"]
    assert document["arithmetic"]["total"] == 275_000
    assert document["arithmetic"]["subtotal"] == 250_000
    assert document["arithmetic"]["tax"] == 25_000
    assert document["arithmetic"]["consistent"] is True
    assert document["findings"] == []


# -- capture time --------------------------------------------------------------


def test_capture_time_is_read_from_exif(clean_receipt: Path):
    metadata = analyse_image(clean_receipt).as_dict()["metadata"]
    assert metadata["captured_at"] == "2026-09-27T14:03:11"
    assert metadata["captured_at_source"] == "exif:DateTimeOriginal"
    assert metadata["exif_fields"]["model"] == "SM-S911B"


def test_screenshot_without_metadata_reports_no_date_and_is_not_called_edited(tmp_path: Path):
    path = tmp_path / "shot.png"
    info = PngInfo()
    info.add_text("Software", "Screenshot")
    _draw(_receipt_lines()).save(path, "PNG", pnginfo=info)

    forensics = analyse_image(path).as_dict()
    verdict = score(forensics, None)
    assert forensics["metadata"]["captured_at"] is None
    # PNG is not expected to carry EXIF, so the absence must not be scored.
    assert forensics["metadata"]["metadata_stripped"] is False
    assert verdict["band"] == "no_visible_tampering"


# -- metadata edit signals -----------------------------------------------------


def test_editor_software_and_late_write_are_flagged(tmp_path: Path):
    img = _draw(_receipt_lines())
    path = _write_jpeg(
        tmp_path / "edited.jpg", img, _exif(img, software="Adobe Photoshop 25.0", written="2026:09:27 19:00:00")
    )
    forensics = analyse_image(path).as_dict()
    verdict = score(forensics, None)

    names = {s["signal"] for s in verdict["signals"]}
    assert "editor_software_named" in names
    assert "exif_written_after_capture" in names
    assert verdict["band"] == "weak_signals"
    assert verdict["confidence"] == "low"


def test_jpeg_without_exif_reports_stripped_metadata(tmp_path: Path):
    path = _write_jpeg(tmp_path / "bare.jpg", _draw(_receipt_lines()))
    metadata = analyse_image(path).as_dict()["metadata"]
    assert metadata["metadata_stripped"] is True


# -- document layer ------------------------------------------------------------


def test_date_digits_are_not_read_as_an_amount():
    assert _parse_number("Date: 27/09/2026 14:03:11") is None
    assert _parse_number("Account: 1234567890") is None
    assert _parse_number("Total: IDR 275.000") == 275_000


@needs_font
@needs_ocr
def test_subtotal_is_not_mistaken_for_the_total(tmp_path: Path):
    """Regression: "Subtotal" contains "total" and used to be read as the grand total."""
    img = _draw(_receipt_lines())
    path = _write_jpeg(tmp_path / "sub.jpg", img, _exif(img))
    document = analyse_document(path)
    arithmetic = document["arithmetic"]
    assert arithmetic["total"] == 275_000
    assert arithmetic["subtotal"] == 250_000
    assert arithmetic["consistent"] is True


@needs_font
@needs_ocr
def test_arithmetic_mismatch_is_caught(tmp_path: Path):
    img = _draw(_receipt_lines(total="IDR 975.000"))
    path = _write_jpeg(tmp_path / "forged_total.jpg", img, _exif(img))

    document = analyse_document(path)
    verdict = score(analyse_image(path).as_dict(), document)

    assert document["arithmetic"]["consistent"] is False
    assert document["arithmetic"]["difference"] == 700_000
    assert any(f["signal"] == "arithmetic" for f in document["findings"])
    assert verdict["band"] in ("signs_of_editing", "strong_signs_of_editing")


@needs_font
@needs_ocr
def test_duplicate_reference_number_is_caught(tmp_path: Path):
    lines = _receipt_lines() + ["Reference: 20260927140311009876"]
    img = _draw(lines)
    path = _write_jpeg(tmp_path / "dup.jpg", img, _exif(img))

    document = analyse_document(path)
    assert document["duplicates"]
    assert any(f["signal"] == "duplicate_reference" for f in document["findings"])


# -- reconciliation ------------------------------------------------------------


def test_reconciliation_mismatch_outranks_every_pixel_signal(clean_receipt: Path):
    forensics = analyse_image(clean_receipt).as_dict()
    document = analyse_document(clean_receipt, expected_amount="10.00")
    verdict = score(forensics, document)

    assert verdict["band"] == "contradicts_issuer_record"
    assert verdict["decisive_signal"] == "issuer_record_mismatch"
    assert verdict["confidence"] == "high"
    assert "reconciliation" in verdict["families"]
    assert any(s["signal"] == "issuer_record_mismatch" for s in verdict["signals"])


def test_reconciliation_match_never_claims_genuine(clean_receipt: Path):
    forensics = analyse_image(clean_receipt).as_dict()
    document = analyse_document(
        clean_receipt, expected_amount="275000", expected_date="2026-09-27"
    )
    verdict = score(forensics, document)

    assert verdict["band"] != "credential_verified"
    assert verdict["cannot_prove_genuine"] is True
    assert any("cannot prove" in limit for limit in verdict["limits"])


def test_unsupplied_reconciliation_is_named_as_a_gap(clean_receipt: Path):
    document = analyse_document(clean_receipt)
    verdict = score(analyse_image(clean_receipt).as_dict(), document)
    assert any("reconciled against the issuer" in limit for limit in verdict["limits"])


# -- provenance and localization honesty ---------------------------------------


def test_absent_c2pa_manifest_is_not_scored(clean_receipt: Path):
    provenance = provenance_check(clean_receipt)
    assert provenance["manifest_present"] is False
    verdict = score(analyse_image(clean_receipt).as_dict(), None)
    kind = {s["signal"]: s for s in verdict["signals"]}
    if "c2pa_absent" in kind:
        assert kind["c2pa_absent"]["direction"] == "no_signal"
        assert kind["c2pa_absent"]["weight"] == 0.0
    assert verdict["positive_score"] == 0.0


def test_repeated_content_gate_rejects_watermark_like_repeats():
    """Many duplicated blocks across an otherwise uniform page must not be flagged."""
    import numpy as np

    copy_move = {
        "duplicate_pairs": 38,
        "uniform_tile_fraction": 0.88,
        "examples": [{"a": {"x": 10, "y": 10}, "b": {"x": 400, "y": 600}}],
    }
    assert duplicate_regions(copy_move, np.zeros((800, 600), dtype=np.float32)) == []

    copy_move_suspicious = {
        "duplicate_pairs": 9,
        "uniform_tile_fraction": 0.10,
        "examples": [{"a": {"x": 32, "y": 64}, "b": {"x": 400, "y": 600}}],
    }
    regions = duplicate_regions(copy_move_suspicious, np.zeros((800, 600), dtype=np.float32))
    assert regions


def test_ela_is_reported_but_never_scored(clean_receipt: Path):
    forensics = analyse_image(clean_receipt).as_dict()
    verdict = score(forensics, None)
    ela = {s["signal"]: s for s in verdict["signals"]}.get("ela_baseline")
    assert ela is not None
    assert ela["direction"] == "no_signal"
    assert ela["weight"] == 0.0


# -- the tool ------------------------------------------------------------------


def _run(tool: MediaForensicsTool, **kwargs):
    return asyncio.run(tool.execute(**kwargs))


@pytest.fixture
def tool(tmp_path: Path):
    instance = MediaForensicsTool()
    instance._workspace = lambda: tmp_path  # type: ignore[method-assign]
    return instance


def test_tool_analyze_returns_band_and_limits(tool, clean_receipt: Path):
    out = str(_run(tool, action="analyze", path=clean_receipt.name))
    assert "no_visible_tampering" in out
    assert "cannot prove" in out.lower()
    assert "What this does not say" in out


def test_tool_timestamps_reports_capture_time(tool, clean_receipt: Path):
    out = str(_run(tool, action="timestamps", path=clean_receipt.name))
    assert "2026-09-27T14:03:11" in out
    assert "exif:DateTimeOriginal" in out


def test_tool_localize_admits_what_it_cannot_do(tool, clean_receipt: Path):
    out = str(_run(tool, action="localize", path=clean_receipt.name))
    assert "-ela.png" in out
    assert "does not work" in out or "not work" in out


def test_tool_analyze_reconciliation_mismatch(tool, clean_receipt: Path):
    out = str(_run(tool, action="analyze", path=clean_receipt.name, expected_amount="10.00"))
    assert "contradicts_issuer_record" in out


def test_tool_compare_points_at_changed_pixels(tool, clean_receipt: Path, tmp_path: Path):
    img = _draw(_receipt_lines(total="IDR 975.000"))
    second = _write_jpeg(tmp_path / "second.jpg", img, _exif(img))
    out = str(
        _run(
            tool,
            action="compare",
            path=clean_receipt.name,
            other_path=second.name,
        )
    )
    assert "differing" in out
    assert "-diff.png" in out


def test_tool_timeline_orders_and_flags_undated(tool, clean_receipt: Path, tmp_path: Path):
    shot = tmp_path / "shot.png"
    _draw(_receipt_lines()).save(shot, "PNG")
    out = str(_run(tool, action="timeline", paths=[clean_receipt.name, shot.name]))
    assert "2026-09-27T14:03:11" in out
    assert "undated" in out


def test_tool_json_output_is_structured(tool, clean_receipt: Path):
    import json

    out = str(_run(tool, action="analyze", path=clean_receipt.name, json_output=True))
    payload = json.loads(out)
    assert payload["verdict"]["band"] == "no_visible_tampering"
    assert payload["verdict"]["cannot_prove_genuine"] is True


def test_tool_refuses_a_path_outside_the_workspace(tool, tmp_path: Path):
    outside = tmp_path.parent / "outside.jpg"
    _write_jpeg(outside, _draw(_receipt_lines()))
    out = str(_run(tool, action="analyze", path=str(outside)))
    assert "outside the workspace" in out


def test_tool_rejects_an_unknown_action(tool, clean_receipt: Path):
    out = str(_run(tool, action="fabricate", path=clean_receipt.name))
    assert "Unknown action" in out
