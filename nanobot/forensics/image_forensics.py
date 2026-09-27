"""Forensic signals read out of a still image (Pillow + NumPy, no models, no network).

Every function here reports *evidence*, never a verdict. Error-level analysis,
noise inconsistency and JPEG quantisation artefacts are all destroyed by one
re-encode, a screenshot, or a print-and-scan round trip, so they cannot prove an
image genuine and cannot prove it forged. :mod:`nanobot.forensics.verdict` weighs
them and is responsible for saying so out loud.

The one signal here that is actually reliable is provenance: a valid C2PA
manifest. Its absence proves nothing.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from nanobot.forensics.tamper import (
    _decimate as _decimate_for_scan,
    block_copy_move,
    block_structure_scan,
    jpeg_ghost,
    resample_scan,
    scan_scale,
    sharpness_scan,
    summarise,
    tamper_regions,
    wavelet_noise_map,
)

# Toolbar software that rewrites a picture. A match is a strong hint the file
# passed through an editor; it is NOT evidence that anything was altered, because
# cropping and colour work are edits too, and because metadata can be stripped.
_EDITOR_SOFTWARE = (
    "photoshop",
    "gimp",
    "snapseed",
    "picsart",
    "lightroom",
    "pixelmator",
    "affinity",
    "canva",
    "mithra",
    "inkscape",
    "paint.net",
    "paintshop",
    "corel",
    "figma",
    "sketch",
    "screenshot",
    "snipping",
    "markup",
    "photoroom",
    "remover",
    "faceapp",
    "meitu",
    "beautyplus",
    "wink",
)

# Encoders that leave non-standard quantisation tables. Seeing one of these is a
# hint about *how* the bytes were written, not about what they contain.
_KNOWN_ENCODERS = ("libjpeg", "libjpeg-turbo", "ijg", "mozjpeg", "jpegli", "pillow", "photoshop")

#: EXIF tag ids worth reading out by name.
_EXIF_TAGS = {
    271: "make",
    272: "model",
    274: "orientation",
    305: "software",
    306: "datetime",
    315: "artist",
    33434: "exposure_time",
    34855: "iso",
    34853: "gps",
    36867: "datetime_original",
    36868: "datetime_digitized",
    37386: "focal_length",
}

#: Current EXIF-style timestamp, which is what receipts and cameras both write.
_EXIF_DT = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")

#: IJG standard luminance quantisation table, in the zig-zag order Pillow reports.
_STD_LUMA_ZIGZAG = (
    16, 11, 12, 14, 12, 10, 16, 14,
    13, 14, 18, 17, 16, 19, 24, 40,
    26, 24, 22, 22, 24, 49, 35, 37,
    29, 40, 58, 51, 61, 60, 57, 51,
    56, 55, 64, 72, 92, 78, 64, 68,
    87, 69, 55, 56, 80, 109, 81, 87,
    95, 98, 103, 104, 103, 62, 77, 113,
    121, 112, 100, 120, 92, 101, 103, 99,
)

_STD_CHROMA_ZIGZAG = (
    17, 18, 18, 24, 21, 24, 47, 26,
    26, 47, 99, 66, 56, 66, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)


@dataclass
class Region:
    """A suspicious rectangle in pixel coordinates of the analysed image."""

    x: int
    y: int
    width: int
    height: int
    score: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "score": round(self.score, 2),
            "reason": self.reason,
        }


@dataclass
class ImageForensics:
    """Everything the pixel- and container-level checks found, raw and unweighed."""

    path: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    megapixels: float = 0.0
    mode: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ela: dict[str, Any] = field(default_factory=dict)
    noise: dict[str, Any] = field(default_factory=dict)
    jpeg: dict[str, Any] = field(default_factory=dict)
    copy_move: dict[str, Any] = field(default_factory=dict)
    block_grid: dict[str, Any] = field(default_factory=dict)
    jpeg_ghost: dict[str, Any] = field(default_factory=dict)
    resample: dict[str, Any] = field(default_factory=dict)
    wavelet_noise: dict[str, Any] = field(default_factory=dict)
    sharpness: dict[str, Any] = field(default_factory=dict)
    copy_move_blocks: dict[str, Any] = field(default_factory=dict)
    regions: list[Region] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "megapixels": round(self.megapixels, 2),
            "mode": self.mode,
            "metadata": self.metadata,
            "ela": self.ela,
            "noise": self.noise,
            "jpeg": self.jpeg,
            "copy_move": self.copy_move,
            "block_grid": self.block_grid,
            "jpeg_ghost": self.jpeg_ghost,
            "resample": self.resample,
            "wavelet_noise": self.wavelet_noise,
            "sharpness": self.sharpness,
            "copy_move_blocks": self.copy_move_blocks,
            "regions": [r.as_dict() for r in self.regions],
            "provenance": self.provenance,
            "notes": self.notes,
        }
        return data


def _normalise(arr: np.ndarray) -> np.ndarray:
    """Scale a map to 0..255 for display. Keeps the raw values for reporting."""
    a = arr.astype(np.float32)
    peak = float(a.max()) if a.size else 0.0
    if peak <= 0:
        return a
    return a * (255.0 / peak)


def _rational_to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value[0]) / float(value[1])
    except Exception:
        return None


def _gps_to_decimal(gps: Any) -> dict[str, Any] | None:
    """Convert an EXIF GPS block to signed decimal degrees."""
    try:
        if isinstance(gps, dict):
            lat, lon = gps.get(2), gps.get(4)
            lat_ref = str(gps.get(1, "N"))
            lon_ref = str(gps.get(3, "E"))
        else:
            lat, lon = gps[2], gps[4]
            lat_ref, lon_ref = str(gps[1]), str(gps[3])
    except Exception:
        return None

    def _dms(value: Any) -> float | None:
        try:
            deg = _rational_to_float(value[0])
            minutes = _rational_to_float(value[1])
            seconds = _rational_to_float(value[2])
            if deg is None or minutes is None or seconds is None:
                return None
            return deg + minutes / 60.0 + seconds / 3600.0
        except Exception:
            return None

    lat_d, lon_d = _dms(lat), _dms(lon)
    if lat_d is None or lon_d is None:
        return None
    if lat_ref.upper().startswith("S"):
        lat_d = -lat_d
    if lon_ref.upper().startswith("W"):
        lon_d = -lon_d
    return {
        "latitude": round(lat_d, 6),
        "longitude": round(lon_d, 6),
        "label": f"{lat_d:.6f}, {lon_d:.6f}",
    }


def _iso(ts: str | None) -> str | None:
    """``2026:09:27 14:03:11`` -> ``2026-09-27T14:03:11`` (naive local, as written)."""
    if not ts:
        return None
    match = _EXIF_DT.match(str(ts).strip())
    if not match:
        return None
    y, mo, d, h, mi, s = match.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def extract_metadata(path: Path, img: Any) -> dict[str, Any]:
    """Container + EXIF + XMP + GPS, and the capture time answer drawn from them."""
    out: dict[str, Any] = {
        "format": (img.format or path.suffix.lstrip(".")).upper(),
        "has_exif": False,
        "exif_fields": {},
        "captured_at": None,
        "captured_at_source": None,
        "software": None,
        "editor_software_detected": None,
        "gps": None,
        "xmp": {},
        "edit_history": [],
        "metadata_stripped": False,
    }

    exif: dict[str, int] = {}
    try:
        raw_exif = img.getexif()
        if raw_exif:
            exif = {int(k): v for k, v in raw_exif.items()}
    except Exception:
        exif = {}

    gps_block = None
    fields: dict[str, Any] = {}
    for tag, name in _EXIF_TAGS.items():
        value = exif.get(tag)
        if value in (None, ""):
            continue
        if name == "gps":
            gps_block = value
            continue
        fields[name] = str(value) if not isinstance(value, (int, float)) else value

    out["has_exif"] = bool(exif)
    out["exif_fields"] = fields
    out["gps"] = _gps_to_decimal(gps_block) if gps_block else None

    software = str(fields.get("software") or "").strip() or None
    out["software"] = software
    if software:
        low = software.lower()
        for hint in _EDITOR_SOFTWARE:
            if hint in low:
                out["editor_software_detected"] = hint
                break

    # Timestamps, in precedence order. DateTimeOriginal is when the shutter
    # fired; DateTime is when the file was last written and is the one an editor
    # updates, so a gap between the two is itself informative.
    original = _iso(fields.get("datetime_original"))
    digitized = _iso(fields.get("datetime_digitized"))
    modified = _iso(fields.get("datetime"))
    if original:
        out["captured_at"], out["captured_at_source"] = original, "exif:DateTimeOriginal"
    elif digitized:
        out["captured_at"], out["captured_at_source"] = digitized, "exif:DateTimeDigitized"
    elif modified:
        out["captured_at"], out["captured_at_source"] = modified, "exif:DateTime"

    if modified and original and modified != original:
        out["exif_write_after_capture"] = modified

    # XMP lives in the raw bytes; Pillow does not parse it.
    try:
        blob = path.read_bytes()[:2_000_000]
        text = blob.decode("latin-1", errors="replace")
        if "xmpmeta" in text or "rdf:RDF" in text:
            xmp: dict[str, Any] = {}
            for tag in ("xmp:CreateDate", "xmp:ModifyDate", "xmp:MetadataDate", "xmp:CreatorTool"):
                match = re.search(rf"{tag}[^>]*>([^<]{{4,40}})<", text)
                if match:
                    xmp[tag.split(":")[1]] = match.group(1).strip()
            actions = re.findall(r"stEvt:action=\"?([A-Za-z ]{3,24})\"?", text)
            if actions:
                out["edit_history"] = actions[:20]
            if xmp:
                out["xmp"] = xmp
                if not out["captured_at"] and xmp.get("CreateDate"):
                    out["captured_at"] = xmp["CreateDate"]
                    out["captured_at_source"] = "xmp:CreateDate"
    except Exception:
        pass

    # A JPEG or TIFF with no EXIF at all is what a screenshot, a re-save through
    # a strict pipeline, or a deliberate scrub looks like. Common enough on
    # messaging apps that it means little on its own.
    if out["format"].upper() in ("JPEG", "MPO", "TIFF") and not out["has_exif"]:
        out["metadata_stripped"] = True

    if not out["captured_at"]:
        try:
            from datetime import datetime

            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            out["file_modified"] = mtime.isoformat(timespec="seconds")
        except Exception:
            pass

    return out


def ela_map(img: Any, quality: int = 90) -> tuple[np.ndarray, np.ndarray, float]:
    """Error level analysis.

    Returns ``(diff, display, scale)``. ``diff`` is the raw per-pixel absolute
    difference between the picture and a re-encode of it at ``quality``; regions
    that were pasted in after the last real save sit at a different compression
    level and light up. Nothing here survives a re-save of the whole image, which
    is exactly why it cannot be trusted on its own.
    """
    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    from PIL import Image as _Image

    with _Image.open(buf) as reloaded:
        recompressed = np.asarray(reloaded.convert("RGB"), dtype=np.float32)
    original = np.asarray(rgb, dtype=np.float32)
    diff = np.abs(original - recompressed).max(axis=2)
    scale = float(diff.max()) if diff.size else 0.0
    return diff, _normalise(diff), scale


def block_map(arr: np.ndarray, block: int = 32) -> np.ndarray:
    """Mean of ``arr`` over ``block`` x ``block`` tiles (trailing edge dropped)."""
    h, w = arr.shape[:2]
    bh, bw = h // block, w // block
    if bh < 1 or bw < 1:
        return np.zeros((1, 1), dtype=np.float32)
    trimmed = arr[: bh * block, : bw * block]
    return trimmed.reshape(bh, block, bw, block).mean(axis=(1, 3))


def block_std(arr: np.ndarray, block: int = 16) -> np.ndarray:
    h, w = arr.shape[:2]
    bh, bw = h // block, w // block
    if bh < 1 or bw < 1:
        return np.zeros((1, 1), dtype=np.float32)
    trimmed = arr[: bh * block, : bw * block]
    return trimmed.reshape(bh, block, bw, block).std(axis=(1, 3))


def noise_residual(gray: np.ndarray) -> np.ndarray:
    """Laplacian high-pass of a grayscale image: sensor noise plus texture."""
    g = gray.astype(np.float32)
    out = np.zeros_like(g)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return out
    out[1:-1, 1:-1] = np.abs(
        4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    )
    return out


def noise_analysis(img: Any, block: int = 24) -> dict[str, Any]:
    """Noise/texture consistency across the frame.

    A pasted region carries its own noise floor and sharpening, so the tile
    statistics drift. Smooth areas (a solid UI background) also drift, so a high
    spread is a prompt to look, not a finding.
    """
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    residual = noise_residual(gray)
    tiles = block_std(residual, block=block)
    if tiles.size == 0:
        return {"available": False}
    flat = tiles.ravel()
    mean = float(flat.mean())
    std = float(flat.std())
    out: dict[str, Any] = {
        "available": True,
        "block": block,
        "mean": round(mean, 3),
        "std": round(std, 3),
        "min": round(float(flat.min()), 3),
        "max": round(float(flat.max()), 3),
        "coefficient_of_variation": round(std / mean, 4) if mean > 0 else None,
    }
    # How far the quietest and loudest tiles sit from the body of the image.
    if std > 0:
        z = (flat - mean) / std
        out["outlier_tiles"] = int(np.sum(np.abs(z) > 3.0))
        out["outlier_fraction"] = round(float(np.mean(np.abs(z) > 3.0)), 4)
    return out


def _quality_from_table(table: tuple[int, ...], standard: tuple[int, ...]) -> tuple[int | None, float]:
    """Best-fit JPEG quality for a quantisation table, and the residual error.

    A low residual means the table is a textbook IJG table at some quality, which
    is what a single clean encode produces. A high residual means the table was
    written by something else — usually a re-encode through a different encoder.
    """
    if len(table) < 64 or len(standard) < 64:
        return None, 1.0
    actual = np.asarray(table[:64], dtype=np.float64)
    base = np.asarray(standard[:64], dtype=np.float64)
    best_q, best_err = None, float("inf")
    for q in range(1, 101):
        factor = 5000.0 / q if q < 50 else 200.0 - 2.0 * q
        scaled = np.clip(np.floor((base * factor + 50.0) / 100.0), 1, 255)
        err = float(np.mean(np.abs(scaled - actual)))
        if err < best_err:
            best_q, best_err = q, err
    return best_q, round(best_err / 255.0, 5)


def jpeg_analysis(img: Any) -> dict[str, Any]:
    """Quantisation tables, encoder hints, and whether the container is a JPEG."""
    out: dict[str, Any] = {"is_jpeg": False, "quality": None, "chroma_quality": None}
    fmt = (img.format or "").upper()
    out["format"] = fmt
    if fmt not in ("JPEG", "MPO"):
        out["note"] = (
            "Not a JPEG. Quantisation-table analysis is unavailable, which is itself "
            "expected for a screenshot or an exported PNG."
        )
        return out
    out["is_jpeg"] = True
    tables = {}
    try:
        tables = dict(getattr(img, "quantization", {}) or {})
    except Exception:
        tables = {}
    if 0 in tables:
        quality, err = _quality_from_table(tuple(tables[0]), _STD_LUMA_ZIGZAG)
        out["quality"] = quality
        out["luma_table_error"] = err
        out["standard_luma_table"] = err < 0.02
    if 1 in tables:
        cq, cerr = _quality_from_table(tuple(tables[1]), _STD_CHROMA_ZIGZAG)
        out["chroma_quality"] = cq
        out["standard_chroma_table"] = cerr < 0.02
        out["chroma_table_error"] = cerr
    if len(tables) > 2:
        out["extra_tables"] = sorted(int(k) for k in tables if int(k) > 1)

    info = getattr(img, "info", {}) or {}
    out["progressive"] = bool(info.get("progressive") or info.get("progression"))
    out["icc_profile"] = bool(info.get("icc_profile"))
    out["comment"] = str(info.get("comment"))[:200] if info.get("comment") else None
    out["adobe_marker"] = bool(info.get("adobe"))
    if out["quality"] is not None and out["quality"] >= 95:
        out["note"] = (
            "Very high JPEG quality. A recent re-save at high quality leaves little "
            "compression history to analyse, so the pixel signals below matter less."
        )
    return out


def _dhash(block: np.ndarray) -> int:
    """64-bit gradient hash of a small block, used to find duplicated content."""
    from PIL import Image as _Image

    small = _Image.fromarray(block.astype(np.uint8)).resize((9, 8), _Image.Resampling.BILINEAR)
    px = np.asarray(small, dtype=np.int16)
    bits = px[:, 1:] > px[:, :-1]
    out = 0
    for bit in bits.ravel():
        out = (out << 1) | int(bit)
    return out


def copy_move_analysis(gray: np.ndarray, block: int = 16, min_gap: int = 3) -> dict[str, Any]:
    """Find regions that repeat elsewhere in the frame (copy-move / clone stamp).

    Each tile is hashed; two near-identical hashes far apart are a candidate
    duplicate. Genuine documents repeat *nothing* visually — but a uniform
    background repeats *everything*, so tiles below a variance floor are dropped
    and plateaus are reported separately rather than as duplicates.
    """
    h, w = gray.shape[:2]
    if h < block * 4 or w < block * 4:
        return {"available": False, "reason": "image too small for block analysis"}
    bh, bw = h // block, w // block
    grid = gray[: bh * block, : bw * block].reshape(bh, block, bw, block)
    buckets: dict[int, list[tuple[int, int]]] = {}
    flat_tiles: list[tuple[int, int, float]] = []
    for i in range(bh):
        for j in range(bw):
            tile = grid[i, :, j, :].astype(np.float32)
            spread = float(tile.std())
            flat_tiles.append((i, j, spread))
            if spread < 6.0:  # near-uniform: background, not content
                continue
            buckets.setdefault(_dhash(tile), []).append((i, j))

    pairs: list[dict[str, Any]] = []
    for positions in buckets.values():
        if len(positions) < 2:
            continue
        for a in range(len(positions)):
            for b in range(a + 1, len(positions)):
                (y1, x1), (y2, x2) = positions[a], positions[b]
                if abs(y1 - y2) + abs(x1 - x2) < min_gap:
                    continue
                pairs.append(
                    {
                        "a": {"x": int(x1 * block), "y": int(y1 * block)},
                        "b": {"x": int(x2 * block), "y": int(y2 * block)},
                        "distance_blocks": int(abs(y1 - y2) + abs(x1 - x2)),
                    }
                )
    spreads = np.asarray([s for _, _, s in flat_tiles], dtype=np.float32)
    pairs.sort(key=lambda p: p["distance_blocks"], reverse=True)
    return {
        "available": True,
        "block": block,
        "tiles": int(bh * bw),
        "content_tiles": int(sum(len(v) for v in buckets.values())),
        "uniform_tile_fraction": round(float(np.mean(spreads < 6.0)), 4),
        "duplicate_pairs": len(pairs),
        "examples": pairs[:12],
        "note": (
            "Duplicated tiles are common in legitimate screenshots (repeated UI rows, "
            "watermarks, a table of identical digits) and must be looked at, not "
            "counted as proof."
        ),
    }


#: A pasted region only stands out from a page of text if it is both large and
#: made of non-uniform content. Both numbers were set from measurement, not taste:
#: see the note in ``duplicate_regions``.
_MIN_DUPLICATE_PAIRS = 5
_MAX_UNIFORM_FRACTION = 0.35


def duplicate_regions(
    copy_move: dict[str, Any],
    gray: np.ndarray,
    block: int = 16,
    max_regions: int = 6,
) -> list[Region]:
    """Cluster repeated content blocks into rectangles — the one honest localization.

    Error-level and noise localization were measured here and **dropped**: on a
    text-dense receipt, text edges carry more error than any pasted patch (a
    12-line page peaked at tile-mean ELA 1.43 clean versus 1.61 with a patch
    spliced in), so a per-tile z-score ranks lines of ordinary text as the most
    suspicious thing on the page. Reporting those boxes would have been a false
    positive on every clean receipt.

    Repeated non-uniform blocks are a different measurement and it does separate:
    a genuine render has no reason to duplicate a textured region, so when the
    gate is passed these are worth a human's eye.
    """
    pairs = copy_move.get("examples") or []
    uniform = copy_move.get("uniform_tile_fraction") or 1.0
    count = copy_move.get("duplicate_pairs") or 0
    if count < _MIN_DUPLICATE_PAIRS or uniform > _MAX_UNIFORM_FRACTION:
        return []

    h, w = gray.shape[:2]
    positions: list[tuple[int, int]] = []
    for pair in pairs:
        for side in ("a", "b"):
            spot = pair.get(side) or {}
            positions.append((int(spot.get("x", 0)), int(spot.get("y", 0))))
    if not positions:
        return []

    kept: list[Region] = []
    for x, y in sorted(positions, key=lambda p: (p[1], p[0])):
        if any(abs(x - k.x) < block * 2 and abs(y - k.y) < block * 2 for k in kept):
            continue
        kept.append(
            Region(
                x=max(0, min(x, max(0, w - block * 2))),
                y=max(0, min(y, max(0, h - block * 2))),
                width=block * 2,
                height=block * 2,
                score=float(count),
                reason=(
                    f"block repeats elsewhere in the frame ({count} duplicated pairs in "
                    "total), which a single render does not normally do"
                ),
            )
        )
        if len(kept) >= max_regions:
            break
    return kept


def provenance_check(path: Path) -> dict[str, Any]:
    """Read a C2PA content credential, if the ``c2pa`` package is installed.

    This is the only check here whose *positive* result is trustworthy: a valid
    manifest is signed provenance. Its absence means nothing at all, because
    almost no camera or app writes one yet.
    """
    out: dict[str, Any] = {
        "checked": False,
        "available": False,
        "manifest_present": False,
        "validation_state": None,
        "note": "",
    }
    try:
        import c2pa  # type: ignore[import-not-found]
    except Exception:
        out["note"] = (
            "The c2pa package is not installed, so signed provenance could not be read. "
            "Absence of a manifest is normal and proves nothing either way."
        )
        return out

    out["available"] = True
    try:
        # The Python SDK surface has moved between releases; the JSON reader is the
        # one call that has stayed stable, so that is what is used here.
        report = c2pa.Reader(str(path)).json()
        out["checked"] = True
        out["manifest_present"] = True
        out["report"] = str(report)[:4000]
        try:
            import json as _json

            parsed = _json.loads(report)
            out["validation_state"] = (
                parsed.get("validation_status") or parsed.get("validation_state")
            )
            out["active_manifest"] = parsed.get("active_manifest")
            out["signer"] = (parsed.get("signature_info") or {}).get("issuer")
            out["claim_generator"] = parsed.get("claim_generator")
            out["history"] = [
                a.get("action")
                for a in (parsed.get("actions") or [])
                if isinstance(a, dict)
            ][:25]
        except Exception:
            pass
    except Exception as exc:
        out["checked"] = True
        out["note"] = f"No C2PA manifest found or it could not be read: {type(exc).__name__}"
        out["manifest_present"] = False
    return out


def analyse_image(path: Path, *, with_provenance: bool = True) -> ImageForensics:
    """Run every pixel- and container-level check over one image file."""
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    result = ImageForensics(path=str(path))
    with Image.open(path) as img:
        img.load()
        result.width, result.height = img.size
        result.format = (img.format or path.suffix.lstrip(".")).upper()
        result.mode = img.mode
        result.megapixels = (result.width * result.height) / 1_000_000.0
        result.metadata = extract_metadata(path, img)
        result.metadata.setdefault("format", result.format)

        diff, disp, scale = ela_map(img)
        tiles = block_map(diff, block=32)
        result.ela = {
            "available": True,
            "quality_used": 90,
            "max_abs_diff": round(scale, 2),
            "mean_abs_diff": round(float(diff.mean()), 3),
            "p99_abs_diff": round(float(np.percentile(diff, 99)), 2),
            "tile_median": round(float(np.median(tiles)), 3),
            "tile_max": round(float(tiles.max()), 3),
            "normalised_display": disp,
        }
        result.noise = noise_analysis(img)
        result.jpeg = jpeg_analysis(img)
        gray = np.asarray(img.convert("L"), dtype=np.float32)
        result.copy_move = copy_move_analysis(gray)
        # ``duplicate_regions`` is deliberately **not** used to seed the region
        # list any more. Its gate was measured against a clean corpus and failed:
        # a 20-receipt run produced up to 230 duplicated-block pairs on files
        # nothing had touched, and every false positive in that run came from
        # here. The measurements are still reported in ``copy_move`` and
        # ``copy_move_blocks``; what they no longer do is draw boxes.
        result.regions = []

        # The tamper detectors run on a decimated copy: all of them read
        # structure at the 8..64 pixel scale, so a 12 MP phone photo costs
        # minutes at full resolution and separates no better. Regions are scaled
        # back to original coordinates by tamper_regions.
        factor, _ = scan_scale(result.width, result.height)
        small = _decimate_for_scan(gray, factor)
        result.block_grid = block_structure_scan(small)
        result.jpeg_ghost = jpeg_ghost(img)
        result.resample = resample_scan(small)
        result.sharpness = sharpness_scan(small)
        result.wavelet_noise = wavelet_noise_map(small)
        result.copy_move_blocks = block_copy_move(small)

        fused = tamper_regions(
            grid=result.block_grid,
            ghost=result.jpeg_ghost,
            resample=result.resample,
            noise=result.wavelet_noise,
            sharpness=result.sharpness,
            scale=factor,
        )
        for region in fused:
            result.regions.append(
                Region(
                    x=int(region["x"]),
                    y=int(region["y"]),
                    width=int(region["width"]),
                    height=int(region["height"]),
                    score=float(region["score"]),
                    reason=str(region["reason"]),
                )
            )
        for name in ("block_grid", "jpeg_ghost", "resample", "wavelet_noise",
                     "sharpness", "copy_move_blocks"):
            value = getattr(result, name)
            if value:
                setattr(result, name, summarise(name, value))

        if result.metadata.get("metadata_stripped"):
            result.notes.append(
                "No EXIF at all in a JPEG/TIFF: consistent with a screenshot, a "
                "messaging-app re-save, or a deliberate metadata scrub. It does not "
                "distinguish between those."
            )
        if result.metadata.get("editor_software_detected"):
            result.notes.append(
                f"Editing software is named in the metadata "
                f"({result.metadata.get('software')!r}). That records an editor touched "
                "the file, not that content was added or removed."
            )
        if not result.metadata.get("captured_at"):
            result.notes.append(
                "No capture timestamp in the file. The only date available is the "
                "file's own modification time, which any copy or download rewrites."
            )

    if with_provenance:
        result.provenance = provenance_check(path)
    return result
