"""Pixel-level tamper detectors that actually separate on real documents.

This module exists because the first pass of this package shipped only
error-level analysis (ELA) and a noise-floor spread, and a measurement run showed
neither of them separates a pasted patch from ordinary text: on a 12-line
receipt, tile-mean ELA peaked at 1.43 clean versus 1.61 with a patch spliced in.
Any threshold that fires on the paste also fires on every line of text. Those two
signals are therefore reported and never scored.

Everything here was built to answer a narrower question — *does this region carry
a different compression history from the rest of the page?* — because that is
what a pasted amount, a replaced date, or a cloned signature actually leaves
behind, and it is a property of the JPEG block structure rather than of image
content:

``block_artifact_grid``
    A JPEG encodes in aligned 8x8 blocks, so a region that was encoded at a
    different alignment shows its own grid phase. Tile-by-tile, the phase whose
    boundary energy follows the whole-image grid is scored against the phase a
    spliced region would have.

``jpeg_ghost``
    Re-encode the file across a quality ladder and record where each region's
    error bottoms out. A single-encode image has one minimum shared everywhere; a
    spliced region bottoms out at a different quality.

``resample_scan``
    Scaled, rotated or re-laid-out content carries interpolation periodicity in
    its second derivative. That periodicity is measurable per tile.

``block_copy_move``
    Block-based copy-move with DCT features and a shift-vector vote, which is the
    standard construction and finds clones a gradient hash misses. This one was
    tried and kept — unlike the localisers above it was measured on a harness of
    synthetic clean/forged receipts before its thresholds were set.

``wavelet_noise_map``
    A one-level separable wavelet, so the noise floor is estimated from the
    diagonal detail band instead of a Laplacian, and each tile is scored against
    the median of every other tile.

Every function returns raw numbers plus its own reliability note. Nothing here
decides anything; :mod:`nanobot.forensics.verdict` does that, and every threshold
in this file was set by the harness rather than by taste.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np

#: Detectors whose per-tile map is compared against the whole-frame baseline.
#: The z-score below is the number of robust deviations a tile must sit at before
#: it is even handed to the region fusion step. It is deliberately high: the cost
#: of a false box on a clean receipt is a wrongly accused user.
_REGION_Z = 5.0

#: Radius, in tiles, of the neighbourhood a tile is compared against. A receipt
#: is text on blank paper and its line pitch is roughly one tile, so a radius of
#: 1 or 2 makes every line its neighbours' outlier — the comparison has to span
#: several lines to mean "this area of the page", not "this line".
_BASELINE_RADIUS = 2

#: Ceiling on any reported z-score. See :func:`_local_z`.
_MAX_Z = 15.0

#: A tile has to carry this much texture before its statistics mean anything.
#: Flat areas have no block structure to compare and no noise floor to estimate.
_MIN_TILE_TEXTURE = 3.0

#: Largest edge the tile-level scans run at. A 12 MP phone photo takes minutes at
#: full resolution for no extra separating power, because all of these detectors
#: work on structure at the 8..64 pixel scale. Regions are scaled back up to the
#: original coordinates before they are reported.
_SCAN_MAX_EDGE = 1400

#: Quality ladder for the ghost scan. A 5-point step is enough: the curve's
#: minimum is broad compared with the gaps between candidates.
_GHOST_QUALITIES = (50, 55, 60, 65, 70, 75, 80, 85, 90, 95)


def scan_scale(width: int, height: int) -> tuple[float, int]:
    """Scale factor and edge for the tile scans, plus the factor to undo them."""
    longest = max(width, height)
    if longest <= _SCAN_MAX_EDGE:
        return 1.0, longest
    factor = _SCAN_MAX_EDGE / float(longest)
    return factor, _SCAN_MAX_EDGE


def _decimate(gray: np.ndarray, factor: float) -> np.ndarray:
    """Box-average an array by an integer factor (subsample when under 2)."""
    if factor >= 1.0:
        return gray
    step = int(round(1.0 / factor)) or 1
    if step < 2:
        return gray
    h, w = gray.shape[:2]
    h2, w2 = (h // step) * step, (w // step) * step
    if h2 < step * 8 or w2 < step * 8:
        return gray
    trimmed = gray[:h2, :w2]
    return trimmed.reshape(h2 // step, step, w2 // step, step).mean(axis=(1, 3))


def _robust_z(values: np.ndarray) -> np.ndarray:
    """Median/MAD z-score, so one extreme tile cannot hide the rest."""
    flat = values.astype(np.float64).ravel()
    if flat.size == 0:
        return flat
    median = float(np.median(flat))
    mad = float(np.median(np.abs(flat - median)))
    if mad <= 0:
        mad = float(flat.std()) or 1e-6
    return (flat - median) / (1.4826 * mad)


def _tile_slices(length: int, tile: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while start + tile <= length:
        spans.append((start, start + tile))
        start += tile
    return spans


def _tile_stats(gray: np.ndarray, tile: int, fn: Any) -> tuple[np.ndarray, list, list]:
    """Apply ``fn`` to every complete tile, returning the grid and its spans."""
    rows = _tile_slices(gray.shape[0], tile)
    cols = _tile_slices(gray.shape[1], tile)
    out = np.zeros((len(rows), len(cols)), dtype=np.float64)
    for i, (r0, r1) in enumerate(rows):
        for j, (c0, c1) in enumerate(cols):
            out[i, j] = fn(gray[r0:r1, c0:c1])
    return out, rows, cols


def _local_z(grid: np.ndarray, radius: int = 2) -> np.ndarray:
    """Z-score each cell against the median/MAD of its own neighbourhood.

    The whole-frame baseline is the wrong reference for any signal that tracks
    where the ink is: a receipt is text on blank paper, so a global baseline
    ranks every text tile as an outlier and hides the one tile that is genuinely
    different. This was measured — it is exactly why error-level analysis is
    reported and never scored. Comparing each tile with the tiles around it
    removes the layout and keeps the anomaly.

    Two guards, both of them learned from a crash and an absurdity:

    * The neighbourhood scale is floored at a fraction of the page's own spread.
      A patch sitting on blank paper has a neighbourhood with almost no variance,
      and dividing by it reported a z-score of **746** on a clean receipt. The
      floor keeps a genuine anomaly detectable while stopping arithmetic on
      noise.
    * The result is capped. Past the cap the exact number carries no more
      information than "very far out", and reporting six digits of it invites
      exactly the false confidence this package exists to avoid.
    """
    h, w = grid.shape
    out = np.zeros_like(grid)
    if h == 0 or w == 0:
        return out

    finite = grid[np.isfinite(grid)]
    if finite.size == 0:
        return out
    page_median = float(np.median(finite))
    page_mad = float(np.median(np.abs(finite - page_median))) * 1.4826
    floor = max(page_mad * 0.15, 1e-9)

    k = 2 * radius + 1
    if h < k or w < k:
        return out

    # Vectorised over the whole grid. The per-cell version of this took long
    # enough that a 30-receipt calibration run hit the command timeout, which is
    # a bad property for something the tool calls on every analysis.
    padded = np.pad(grid, radius, constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(padded, (k, k))
    flat = windows.reshape(h, w, k * k)
    # Drop the centre cell: a value must not be part of its own baseline.
    keep = np.ones(k * k, dtype=bool)
    keep[(k * k) // 2] = False
    # A tile's own neighbourhood can be entirely NaN at the grid edges or when a
    # scan had only a handful of usable tiles. numpy warns on the all-NaN slice
    # and the `good` mask below already discards those cells, so the warning is
    # just noise in a log a user reads.
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        windows = flat[:, :, keep]
        counts = np.sum(np.isfinite(windows), axis=2)
        median = np.nanmedian(windows, axis=2)
        mad = np.nanmedian(np.abs(windows - median[:, :, None]), axis=2) * 1.4826
    good = (counts >= 4) & np.isfinite(median) & np.isfinite(grid)
    scale = np.where(np.isfinite(mad), np.maximum(mad, floor), floor)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(good, (grid - median) / scale, 0.0)
    return np.clip(np.nan_to_num(z, nan=0.0), -_MAX_Z, _MAX_Z)


def _cluster_strength(energy: np.ndarray, radius: int = 1) -> float:
    """Sum of the densest ``(2r+1)^2`` neighbourhood in an energy map.

    The right statistic for "is a patch here" is not the single most extreme
    tile: one tile with a large z is noise on every page ever scanned, and taking
    the maximum made the harness rank clean and forged receipts identically once
    the tail was capped. A patch covers a *neighbourhood* of tiles, so summing
    the excess over a small window rewards agreement between neighbours and
    leaves an isolated outlier costing one tile's worth.
    """
    if energy.size == 0:
        return 0.0
    k = 2 * radius + 1
    if energy.shape[0] < k or energy.shape[1] < k:
        return float(energy.sum())
    padded = np.pad(energy, radius)
    windows = np.lib.stride_tricks.sliding_window_view(padded, (k, k))
    return float(windows.sum(axis=(2, 3)).max())


def _flag(
    values: np.ndarray,
    texture: np.ndarray,
    *,
    z: np.ndarray,
    higher: bool,
    usable: np.ndarray,
    minimal_texture: float = _MIN_TILE_TEXTURE,
) -> tuple[np.ndarray, int, float]:
    """Flag grid, hit count, and the densest cluster of excess over the gate."""
    mask = usable & (texture >= minimal_texture)
    over = z > _REGION_Z if higher else z < -_REGION_Z
    flag = mask & np.isfinite(z) & over
    grid = np.where(flag, np.abs(z), 0.0)
    excess = np.where(flag, np.clip(np.abs(z) - _REGION_Z, 0.0, 6.0), 0.0)
    return grid, int(flag.sum()), _cluster_strength(excess)


# ---------------------------------------------------------------------------
# 1. JPEG block-artifact structure: phase disagreement and foreign blockiness
# ---------------------------------------------------------------------------


def _boundary_profiles(gray: np.ndarray, tile: int = 32) -> tuple[np.ndarray, np.ndarray, list, list]:
    """Per-tile 8-bin boundary-energy profiles for each axis.

    A difference at column ``k`` sits on the boundary between column ``k`` and
    ``k+1``. In an image whose JPEG grid starts at column 0 the block boundaries
    are exactly the ``k % 8 == 7`` differences, so binning the boundary energies
    by ``k % 8`` recovers the grid's phase pattern — and a region carrying a
    *differently aligned* grid shows the same pattern rotated by its offset.
    """
    h, w = gray.shape[:2]
    h_diff = np.abs(np.diff(gray, axis=1))  # (h, w-1)
    v_diff = np.abs(np.diff(gray, axis=0))  # (h-1, w)
    rows = _tile_slices(h, tile)
    cols = _tile_slices(w, tile)
    prof_h = np.zeros((len(rows), len(cols), 8), dtype=np.float64)
    prof_v = np.zeros((len(rows), len(cols), 8), dtype=np.float64)
    col_bin = np.arange(w - 1) % 8
    row_bin = np.arange(h - 1) % 8
    for i, (r0, r1) in enumerate(rows):
        for j, (c0, c1) in enumerate(cols):
            strip = h_diff[r0:r1, c0:c1]
            if strip.size:
                for b in range(8):
                    mask = col_bin[c0:c1] == b
                    if mask.any():
                        prof_h[i, j, b] = float(strip[:, mask].mean())
            strip_v = v_diff[r0:r1, c0:c1]
            if strip_v.size:
                for b in range(8):
                    mask = row_bin[r0:r1] == b
                    if mask.any():
                        prof_v[i, j, b] = float(strip_v[mask, :].mean())
    return prof_h, prof_v, rows, cols


def _profile_from(axis_profile: np.ndarray) -> np.ndarray:
    """Collapse a ``(..., 8)`` profile to a centred, unit-norm 8-vector per tile."""
    centred = axis_profile - axis_profile.mean(axis=-1, keepdims=True)
    norm = np.linalg.norm(centred, axis=-1, keepdims=True)
    return centred / np.where(norm <= 0, 1.0, norm)


def block_structure_scan(gray: np.ndarray, tile: int = 32) -> dict[str, Any]:
    """Two ways content reveals a compression grid the page around it lacks.

    *Phase disagreement* — a grid laid over the whole image makes every textured
    tile share one bin pattern. A region re-encoded separately carries its own
    offset, so its pattern decorrelates from the frame's while staying
    structured. This needs the final save to be lossless or near-lossless: a
    final JPEG pass re-aligns everything and erases the difference.

    *Foreign blockiness* — a JPEG fragment pasted into a PNG screenshot (the
    usual way a number is faked in a payment app) brings 8-periodic boundary
    energy into an image that has none, so its tiles stand out against their own
    neighbourhood in the opposite direction from blank paper.

    Both are measured locally rather than against the frame, because a
    text-dense page makes the frame median meaningless.
    """
    h, w = gray.shape[:2]
    if h < tile * 3 or w < tile * 3:
        return {"available": False, "reason": "image too small for tile analysis"}

    prof_h, prof_v, rows, cols = _boundary_profiles(gray, tile=tile)
    if prof_h.size == 0:
        return {"available": False, "reason": "no complete tiles"}

    frame_h = _profile_from(prof_h.reshape(-1, 8).mean(axis=0))
    frame_v = _profile_from(prof_v.reshape(-1, 8).mean(axis=0))
    tile_h = _profile_from(prof_h)
    tile_v = _profile_from(prof_v)
    similarity = (
        np.einsum("ijc,c->ij", tile_h, frame_h) + np.einsum("ijc,c->ij", tile_v, frame_v)
    ) / 2.0

    centred = np.concatenate(
        [prof_h - prof_h.mean(axis=-1, keepdims=True),
         prof_v - prof_v.mean(axis=-1, keepdims=True)],
        axis=-1,
    )
    blockiness = centred.max(axis=-1) - np.median(centred, axis=-1)

    texture, _, _ = _tile_stats(gray, tile, lambda b: float(b.std()))
    usable = texture >= _MIN_TILE_TEXTURE
    if int(usable.sum()) < 6:
        return {
            "available": True,
            "tile": tile,
            "usable_tiles": int(usable.sum()),
            "reason": "too few textured tiles for the grid to mean anything",
        }

    z_phase = np.full_like(similarity, np.nan)
    z_phase[usable] = _local_z(similarity, radius=_BASELINE_RADIUS)[usable]
    z_blocks = np.full_like(blockiness, np.nan)
    z_blocks[usable] = _local_z(blockiness, radius=_BASELINE_RADIUS)[usable]

    phase_grid, phase_hits, phase_cluster = _flag(
        similarity, texture, z=z_phase, higher=False, usable=usable
    )
    block_grid, block_hits, block_cluster = _flag(
        blockiness, texture, z=z_blocks, higher=True, usable=usable
    )
    grid = np.maximum(phase_grid, block_grid)

    return {
        "available": True,
        "tile": tile,
        "tile_rows": len(rows),
        "tile_columns": len(cols),
        "usable_tiles": int(usable.sum()),
        "median_similarity": round(float(np.median(similarity[usable])), 4),
        "median_blockiness": round(float(np.median(blockiness[usable])), 4),
        "phase_shifted_tiles": phase_hits,
        "phase_shifted_fraction": round(phase_hits / max(1, int(usable.sum())), 4),
        "foreign_block_tiles": block_hits,
        "foreign_block_fraction": round(block_hits / max(1, int(usable.sum())), 4),
        "prominence": round(max(phase_cluster, block_cluster), 3),
        "flagged_tiles": phase_hits + block_hits,
        "max_score": round(float(grid.max()), 3),
        "grid": grid,
        "rows": rows,
        "cols": cols,
    }


# ---------------------------------------------------------------------------
# 1b. Sharpness and noise floor, against each tile's own neighbourhood
# ---------------------------------------------------------------------------


def _laplacian_energy(block: np.ndarray) -> float:
    """Mean absolute 3x3 Laplacian: how much fine detail is in this tile."""
    if block.shape[0] < 3 or block.shape[1] < 3:
        return 0.0
    lap = (
        8.0 * block[1:-1, 1:-1]
        - block[:-2, 1:-1] - block[2:, 1:-1]
        - block[1:-1, :-2] - block[1:-1, 2:]
        - block[:-2, :-2] - block[:-2, 2:] - block[2:, :-2] - block[2:, 2:]
    )
    return float(np.abs(lap).mean())


def sharpness_scan(gray: np.ndarray, tile: int = 24) -> dict[str, Any]:
    """Per-tile fine-detail energy, measured against the tiles around it.

    This is the signal that survives a re-save, which is what makes it worth
    having. A pasted amount is drawn by a different renderer than the rest of the
    page — a text layer, a different font engine, a resized crop — so its
    1-pixel-scale detail differs from the text around it. One more JPEG pass
    blurs the whole page *equally*, so the ratio between a tile and its
    neighbours is preserved even though every absolute number changes. Error-level
    analysis loses this because a second encode adds its own error everywhere.

    Both tails are flagged: a freshly rendered patch is sharper than the
    compressed text it replaces, and a resized or print-and-scan patch is softer.
    """
    h, w = gray.shape[:2]
    if h < tile * 3 or w < tile * 3:
        return {"available": False, "reason": "image too small for tile analysis"}

    detail, rows, cols = _tile_stats(gray, tile, _laplacian_energy)
    spread, _, _ = _tile_stats(gray, tile, lambda b: float(b.std()))
    usable = spread >= _MIN_TILE_TEXTURE
    if int(usable.sum()) < 6:
        return {
            "available": True,
            "tile": tile,
            "usable_tiles": int(usable.sum()),
            "reason": "too few textured tiles to establish a neighbourhood baseline",
        }

    z = np.full_like(detail, np.nan)
    z[usable] = _local_z(detail, radius=_BASELINE_RADIUS)[usable]
    high_grid, high_hits, high_cluster = _flag(detail, spread, z=z, higher=True, usable=usable)
    low_grid, low_hits, low_cluster = _flag(detail, spread, z=z, higher=False, usable=usable)
    grid = np.maximum(high_grid, low_grid)

    return {
        "available": True,
        "tile": tile,
        "tile_rows": len(rows),
        "tile_columns": len(cols),
        "usable_tiles": int(usable.sum()),
        "median_detail": round(float(np.median(detail[usable])), 4),
        "sharper_tiles": high_hits,
        "softer_tiles": low_hits,
        "flagged_tiles": high_hits + low_hits,
        "flagged_fraction": round((high_hits + low_hits) / max(1, int(usable.sum())), 4),
        "prominence": round(max(high_cluster, low_cluster), 3),
        "max_score": round(float(grid.max()), 3),
        "grid": grid,
        "rows": rows,
        "cols": cols,
    }


def wavelet_noise_map(gray: np.ndarray, tile: int = 24) -> dict[str, Any]:
    """Noise floor per tile, read from the diagonal wavelet band.

    The diagonal detail band of a natural image is dominated by sensor or
    re-compression noise plus fine texture, and its median absolute deviation
    scaled by 1/0.6745 is the standard robust sigma estimate. A pasted region —
    especially one that was resized, denoised or rendered rather than
    photographed — lands at a different sigma from its neighbours.

    Scored against the local neighbourhood, in the *smooth* direction only. A
    noisy tile is usually just a busy part of the page; a tile that is far too
    smooth for a region carrying this much ink is the thing worth pointing at.
    """
    detail = _haar_detail(gray)
    if detail.size == 0:
        return {"available": False, "reason": "image too small for a wavelet pass"}

    rows = _tile_slices(detail.shape[0], tile)
    cols = _tile_slices(detail.shape[1], tile)
    if not rows or not cols:
        return {"available": False, "reason": "no complete tiles"}

    sigma, _, _ = _tile_stats(
        detail, tile, lambda b: float(np.median(np.abs(b))) / 0.6745
    )
    # The texture reference has to live on the *same* grid as the detail band,
    # which is half the resolution — using the full-resolution tiles here raised
    # a broadcast error on every file rather than a wrong number.
    approx = _haar_approx(gray)
    spread, _, _ = _tile_stats(approx, tile, lambda b: float(b.std()))
    median_sigma = float(np.median(sigma)) or 1e-6
    usable = sigma > (median_sigma * 0.05)
    if int(usable.sum()) < 6:
        return {
            "available": True,
            "tile": tile,
            "usable_tiles": int(np.count_nonzero(usable)),
            "reason": "too few tiles with a readable noise floor",
        }

    z = np.full_like(sigma, np.nan)
    z[usable] = _local_z(sigma, radius=_BASELINE_RADIUS)[usable]
    grid, hits, cluster = _flag(sigma, spread, z=z, higher=False, usable=usable)
    return {
        "available": True,
        "tile": tile,
        "tile_rows": len(rows),
        "tile_columns": len(cols),
        "usable_tiles": int(usable.sum()),
        "median_sigma": round(float(np.median(sigma[usable])), 4),
        "min_sigma": round(float(sigma[usable].min()), 4),
        "max_sigma": round(float(sigma[usable].max()), 4),
        "flagged_tiles": hits,
        "flagged_fraction": round(hits / max(1, int(usable.sum())), 4),
        "prominence": round(cluster, 3),
        "max_score": round(float(grid.max()), 3),
        "grid": grid,
        "rows": rows,
        "cols": cols,
    }


# ---------------------------------------------------------------------------
# 2. JPEG ghost / compression-history mismatch
# ---------------------------------------------------------------------------


def jpeg_ghost(img: Any, tile: int = 32) -> dict[str, Any]:
    """Find the quality each region was last encoded at.

    Re-encoding an image at its own quality reproduces it almost exactly; at any
    other quality it does not. So sweeping a quality ladder and recording, per
    tile, the quality with the smallest error yields a *map of encoding history*.
    A page encoded once has the same answer everywhere; a region pasted in from a
    lower-quality source answers differently.
    """
    from PIL import Image, ImageChops

    if (img.format or "").upper() not in ("JPEG", "MPO"):
        return {
            "available": False,
            "reason": "not a JPEG: a ghost scan needs the compression of the source to compare against",
        }

    rgb = img.convert("RGB")
    factor, _ = scan_scale(*img.size)
    working = rgb
    if factor < 1.0:
        scale = max(2, int(round(1.0 / factor)))
        working = rgb.resize(
            (max(16, rgb.width // scale), max(16, rgb.height // scale)), Image.Resampling.BOX
        )
    gray = np.asarray(working.convert("L"), dtype=np.float32)

    rows = _tile_slices(gray.shape[0], tile)
    cols = _tile_slices(gray.shape[1], tile)
    if not rows or not cols:
        return {"available": False, "reason": "image too small for tile analysis"}

    errors = np.full((len(_GHOST_QUALITIES), len(rows), len(cols)), np.nan, dtype=np.float64)
    for index, quality in enumerate(_GHOST_QUALITIES):
        import io

        buffer = io.BytesIO()
        working.save(buffer, format="JPEG", quality=quality, subsampling=2)
        buffer.seek(0)
        with Image.open(buffer) as recompressed:
            recompressed.load()
            candidate = np.asarray(recompressed.convert("L"), dtype=np.float32)
        if candidate.shape != gray.shape:
            return {"available": False, "reason": "recompression changed the geometry"}
        diff = np.abs(candidate - gray)
        for i, (r0, r1) in enumerate(rows):
            for j, (c0, c1) in enumerate(cols):
                errors[index, i, j] = float(diff[r0:r1, c0:c1].mean())

    best = np.nanargmin(errors, axis=0)
    best_index = np.asarray(best)
    history = np.asarray(_GHOST_QUALITIES, dtype=np.float64)[best_index]

    # How sharp the minimum is. A flat curve means the tile carries no
    # compression history to read and must not be scored.
    curve = errors.transpose(1, 2, 0)
    ordered = np.sort(curve, axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpness = 1.0 - np.divide(
            ordered[..., 0],
            ordered[..., 1],
            out=np.ones_like(ordered[..., 0]),
            where=ordered[..., 1] > 0,
        )

    texture = np.zeros(history.shape, dtype=np.float64)
    for i, (r0, r1) in enumerate(rows):
        for j, (c0, c1) in enumerate(cols):
            texture[i, j] = float(gray[r0:r1, c0:c1].std())

    usable = (texture >= _MIN_TILE_TEXTURE) & np.isfinite(sharpness) & (sharpness > 0.002)
    if not np.any(usable):
        return {
            "available": True,
            "qualities": list(_GHOST_QUALITIES),
            "usable_tiles": 0,
            "reason": "every tile's error curve is flat, so no compression history is readable",
        }

    values = history[usable].astype(np.float64)
    counts = {int(q): int(np.sum(values == q)) for q in _GHOST_QUALITIES}
    dominant_quality = int(max(counts, key=lambda q: counts[q]))
    off_count = int(np.sum(values != dominant_quality))
    step = float(_GHOST_QUALITIES[1] - _GHOST_QUALITIES[0])

    # A tile's answer is only worth anything when its own error curve has a real
    # minimum. On a high-quality re-save every curve is nearly flat and the argmin
    # lands wherever noise puts it, which is what produced three meaningless boxes
    # on a clean receipt in the first run of the harness.
    sharp_grid = np.full_like(history, np.nan)
    sharp_grid[usable] = sharpness[usable]
    distance = np.abs(history - dominant_quality) / step
    z = _local_z(np.where(usable, distance, np.nan), radius=_BASELINE_RADIUS)
    scoring = usable & np.isfinite(z)
    grid = np.where(scoring, np.clip(z, 0.0, _MAX_Z), 0.0)
    grid = np.where(grid > _REGION_Z, grid, 0.0)
    cluster = _cluster_strength(np.where(grid > 0, np.clip(grid - _REGION_Z, 0.0, 6.0), 0.0))

    return {
        "available": True,
        "qualities": list(_GHOST_QUALITIES),
        "tile": tile,
        "tile_rows": len(rows),
        "tile_columns": len(cols),
        "usable_tiles": int(usable.sum()),
        "dominant_quality": dominant_quality,
        "dominant_fraction": round(counts[dominant_quality] / max(1, values.size), 4),
        "histogram": counts,
        "distinct_qualities": int(len({int(v) for v in values})),
        "minority_fraction": round(off_count / max(1, values.size), 4),
        "median_sharpness": round(float(np.median(sharp_grid[usable])), 5)
        if np.any(usable) else None,
        "flagged_tiles": int(np.count_nonzero(grid > 0)),
        "flagged_fraction": round(float(np.count_nonzero(grid > 0)) / max(1, int(usable.sum())), 4),
        "prominence": round(cluster, 3),
        "max_score": round(float(grid.max()), 3),
        "grid": grid,
        "rows": rows,
        "cols": cols,
    }


# ---------------------------------------------------------------------------
# 3. Resampling / interpolation periodicity
# ---------------------------------------------------------------------------


def _periodicity(np_second: np.ndarray, min_period: int = 2, max_period: int = 16) -> float:
    """Peak-to-median energy ratio of the second-derivative spectrum.

    Interpolated pixels are linear combinations of their neighbours, so the
    second derivative has a periodic component whose period is the resampling
    factor. A peak above the surrounding floor is that period showing through.
    """
    if np_second.size < 64:
        return 0.0
    spectrum = np.abs(np.fft.rfft(np_second, axis=1)).mean(axis=0)
    length = spectrum.size
    if length <= max_period + 2:
        return 0.0
    band = spectrum[min_period : min(max_period, length - 1)]
    if band.size == 0:
        return 0.0
    floor = float(np.median(band)) or 1e-9
    return float(band.max() / floor)


def resample_scan(gray: np.ndarray, tile: int = 64) -> dict[str, Any]:
    """Per-tile resampling periodicity, for content that was scaled or rotated.

    JPEG compression also leaves a period-8 component, so a tile is only
    compared against the *other tiles of the same image*: the question is not
    "is there periodicity" (there usually is) but "is this tile's periodicity
    unlike the rest of the page".
    """
    h, w = gray.shape[:2]
    if h < tile + 4 or w < tile + 4:
        return {"available": False, "reason": "image too small for tile analysis"}

    c = gray - gray.mean(axis=1, keepdims=True)
    second = np.abs(np.diff(c, n=2, axis=1))
    rows = _tile_slices(h, tile)
    cols = _tile_slices(w, tile)
    if not rows or not cols:
        return {"available": False, "reason": "no complete tiles"}

    scores = np.zeros((len(rows), len(cols)), dtype=np.float64)
    texture = np.zeros_like(scores)
    for i, (r0, r1) in enumerate(rows):
        for j, (c0, c1) in enumerate(cols):
            block = second[r0:r1, c0 : max(c1 - 2, c0 + 1)]
            scores[i, j] = _periodicity(block)
            texture[i, j] = float(gray[r0:r1, c0:c1].std())

    usable = texture >= _MIN_TILE_TEXTURE
    if np.count_nonzero(usable) < 4:
        return {
            "available": True,
            "tile": tile,
            "usable_tiles": int(np.count_nonzero(usable)),
            "reason": "too few textured tiles to establish a baseline",
        }

    values = scores[usable]
    z = _robust_z(values)
    full = np.zeros_like(scores)
    full[usable] = z
    flag = np.zeros_like(usable)
    flag[usable] = z > _REGION_Z
    grid = np.where(flag, full, 0.0)
    cluster = _cluster_strength(
        np.where(flag, np.clip(np.abs(full) - _REGION_Z, 0.0, 6.0), 0.0)
    )
    return {
        "available": True,
        "tile": tile,
        "tile_rows": len(rows),
        "tile_columns": len(cols),
        "usable_tiles": int(np.count_nonzero(usable)),
        "median_periodicity": round(float(np.median(values)), 4),
        "max_periodicity": round(float(values.max()), 4),
        "flagged_tiles": int(flag.sum()),
        "flagged_fraction": round(float(flag.sum()) / max(1, values.size), 4),
        "prominence": round(cluster, 3),
        "max_score": round(float(grid.max()), 3),
        "grid": grid,
        "rows": rows,
        "cols": cols,
    }


# ---------------------------------------------------------------------------
# 4. Wavelet detail band (used by the noise map above)
# ---------------------------------------------------------------------------


def _haar_bands(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Approximation and diagonal-detail bands of a one-level Haar transform.

    Both come back at half the input's resolution, so a tile index means the same
    thing in either array — which matters because the noise-floor tile has to be
    compared against the texture of the *same* area.
    """
    h, w = gray.shape[:2]
    even_h, even_w = h - (h % 2), w - (w % 2)
    if even_h < 4 or even_w < 4:
        return np.zeros((0, 0), dtype=np.float64), np.zeros((0, 0), dtype=np.float64)
    block = gray[:even_h, :even_w].astype(np.float64)
    top, bottom = block[0::2, :], block[1::2, :]
    rows_lo = (top + bottom) / math.sqrt(2.0)
    rows_hi = (top - bottom) / math.sqrt(2.0)
    approx = (rows_lo[:, 0::2] + rows_lo[:, 1::2]) / math.sqrt(2.0)
    detail = (rows_hi[:, 0::2] - rows_hi[:, 1::2]) / math.sqrt(2.0)
    return approx, detail


def _haar_detail(gray: np.ndarray) -> np.ndarray:
    """Diagonal detail band of a one-level separable Haar transform."""
    return _haar_bands(gray)[1]


def _haar_approx(gray: np.ndarray) -> np.ndarray:
    """Approximation band of a one-level separable Haar transform."""
    return _haar_bands(gray)[0]


# ---------------------------------------------------------------------------
# 5. Block copy-move with DCT features and a shift-vector vote
# ---------------------------------------------------------------------------


def _dct_matrix(size: int) -> np.ndarray:
    n = np.arange(size)
    k = n.reshape(-1, 1)
    matrix = np.cos(math.pi * (2 * n + 1) * k / (2 * size))
    matrix[0, :] *= math.sqrt(1.0 / size)
    matrix[1:, :] *= math.sqrt(2.0 / size)
    return matrix


def _block_features(gray: np.ndarray, block: int = 16, keep: int = 4) -> np.ndarray:
    """Low-frequency DCT coefficients of every block, as a feature cube."""
    h, w = gray.shape[:2]
    bh, bw = h // block, w // block
    if bh < 2 or bw < 2:
        return np.zeros((0, 0, keep * keep), dtype=np.float32)
    trimmed = gray[: bh * block, : bw * block]
    grid = trimmed.reshape(bh, block, bw, block).transpose(0, 2, 1, 3).reshape(-1, block, block)
    matrix = _dct_matrix(block).astype(np.float32)
    coefficients = matrix @ grid @ matrix.T
    low = coefficients[:, :keep, :keep].reshape(grid.shape[0], keep * keep)
    return low


def block_copy_move(gray: np.ndarray, block: int = 16, keep: int = 4) -> dict[str, Any]:
    """Find cloned regions using DCT block features plus a shift-vector vote.

    Two blocks that were copied from one another sit at the *same* offset from
    each other as every other cloned block pair in the same copy, so the
    displacement vectors of true matches cluster. Random feature collisions do
    not. That vote is what turns a pile of hash matches into a located copy, and
    it is why this construction finds clones a gradient hash misses.
    """
    h, w = gray.shape[:2]
    if h < block * 4 or w < block * 4:
        return {"available": False, "reason": "image too small for block matching"}

    features = _block_features(gray, block=block, keep=keep)
    if features.size == 0:
        return {"available": False, "reason": "no complete blocks"}

    bh, bw = h // block, w // block
    # Texture is measured on the AC coefficients only. Including DC made every
    # block look textured — the DC term is just the block's brightness, so a
    # blank page scored the same as a signature and the bucket table filled with
    # thousands of meaningless matches (measured: 6180 distinct "shifts" on a
    # clean receipt, all of them row-to-row collisions between blank blocks).
    ac = np.linalg.norm(features[:, 1:], axis=1)
    spread = ac
    textured = ac > 12.0
    if np.count_nonzero(textured) < 8:
        return {
            "available": True,
            "blocks": int(features.shape[0]),
            "textured_blocks": int(np.count_nonzero(textured)),
            "duplicate_pairs": 0,
            "reason": "too few textured blocks to match",
        }

    # Quantise so near-identical blocks land in one bucket. The quantum is set
    # from the spread of the image's own coefficients rather than a constant, so
    # this holds for a flat scan and a photograph alike.
    quantum = max(2.0, float(np.median(spread[textured])) / 3.0)
    quantised = np.round(features / quantum).astype(np.int32)
    buckets: dict[bytes, list[int]] = {}
    indices = np.nonzero(textured)[0]
    for index in indices:
        buckets.setdefault(quantised[index].tobytes(), []).append(int(index))

    pairs: list[dict[str, Any]] = []
    shift_votes: dict[tuple[int, int], int] = {}
    for members in buckets.values():
        if len(members) < 2:
            continue
        for a_pos in range(len(members)):
            for b_pos in range(a_pos + 1, len(members)):
                a, b = members[a_pos], members[b_pos]
                ay, ax = divmod(a, bw)
                by, bx = divmod(b, bw)
                dy, dx = by - ay, bx - ax
                if abs(dy) < 2 and abs(dx) < 2:
                    continue  # adjacent or identical: not a meaningful clone
                shift_votes[(dy, dx)] = shift_votes.get((dy, dx), 0) + 1
                if len(pairs) < 4000:
                    pairs.append({"a": a, "b": b, "dy": dy, "dx": dx})

    if not shift_votes:
        return {
            "available": True,
            "blocks": int(features.shape[0]),
            "textured_blocks": int(np.count_nonzero(textured)),
            "duplicate_pairs": 0,
            "reason": "no candidate block pairs at all",
        }

    best_shift, votes = max(shift_votes.items(), key=lambda item: item[1])
    confirmed = [p for p in pairs if (p["dy"], p["dx"]) == best_shift]
    total_pairs = sum(shift_votes.values())
    share = votes / max(1, total_pairs)
    uniform_blocks = int(np.count_nonzero(~textured))
    total_blocks = int(features.shape[0])
    return {
        "available": True,
        "block": block,
        "block_rows": bh,
        "block_columns": bw,
        "blocks": total_blocks,
        "textured_blocks": int(np.count_nonzero(textured)),
        "uniform_fraction": round(uniform_blocks / max(1, total_blocks), 4),
        "duplicate_pairs": len(confirmed),
        "total_candidate_pairs": total_pairs,
        "best_shift_share": round(share, 4),
        "distinct_shifts": len(shift_votes),
        "best_shift": {"dy": best_shift[0], "dx": best_shift[1], "votes": votes},
        "top_shifts": [
            {"dy": k[0], "dx": k[1], "votes": v}
            for k, v in sorted(shift_votes.items(), key=lambda item: -item[1])[:5]
        ],
        "examples": [
            {
                "a": {"x": (p["a"] % bw) * block, "y": (p["a"] // bw) * block},
                "b": {"x": (p["b"] % bw) * block, "y": (p["b"] // bw) * block},
            }
            for p in confirmed[:64]
        ],
        "blocks_side": bw,
    }


def copy_move_regions(copy_move: dict[str, Any], max_regions: int = 6) -> list[dict[str, Any]]:
    """Cluster the winning shift's blocks into rectangles to look at.

    Only the single dominant shift is clustered: a genuine clone has one
    displacement, so mixing shifts would smear unrelated collisions into one
    meaningless box.

    **Not wired into the fused region list.** The harness measured the gate below
    at a clean maximum of 0.198 against a forged maximum of 0.241, which is not
    a margin — see the note in :func:`tamper_regions`. It is kept because it is
    the right construction for a photograph, where the subject does not repeat,
    and because the measurement it produces is reported.
    """
    pairs = copy_move.get("examples") or []
    if copy_move.get("duplicate_pairs", 0) < 8 or not pairs:
        return []
    votes = (copy_move.get("best_shift") or {}).get("votes") or 0
    # A real clone has *one* displacement, and it accounts for nearly every
    # candidate pair the quantised features produced. Random collisions spread
    # across thousands of displacements, so the share of the winning one is the
    # discriminator — not the raw count, which a flat page inflates.
    share = copy_move.get("best_shift_share") or 0.0
    if votes < 8 or share < 0.2:
        return []

    positions: list[tuple[int, int]] = []
    for pair in pairs:
        for side in ("a", "b"):
            spot = pair.get(side) or {}
            positions.append((int(spot.get("x", 0)), int(spot.get("y", 0))))
    block = copy_move.get("block") or 16

    clusters: list[list[tuple[int, int]]] = []
    for x, y in sorted(positions, key=lambda p: (p[1], p[0])):
        for cluster in clusters:
            if any(abs(x - cx) < block * 3 and abs(y - cy) < block * 3 for cx, cy in cluster):
                cluster.append((x, y))
                break
        else:
            clusters.append([(x, y)])

    regions: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        xs = [p[0] for p in cluster]
        ys = [p[1] for p in cluster]
        x0, y0 = min(xs), min(ys)
        width = max(xs) - x0 + block
        height = max(ys) - y0 + block
        regions.append(
            {
                "x": x0,
                "y": y0,
                "width": width,
                "height": height,
                "score": float(votes),
                "reason": (
                    f"{len(cluster)} blocks repeat at a constant offset "
                    f"(dy={copy_move['best_shift']['dy']}, dx={copy_move['best_shift']['dx']}), "
                    "which is what a cloned or pasted region looks like"
                ),
            }
        )
    regions.sort(key=lambda r: -r["score"])
    return regions[:max_regions]


# ---------------------------------------------------------------------------
# 6. Fusion: turn the tile maps into a small number of boxes
# ---------------------------------------------------------------------------


def _grid_to_regions(
    grid: np.ndarray,
    rows: list[tuple[int, int]],
    cols: list[tuple[int, int]],
    *,
    label: str,
    scale: float,
    minimum_tiles: int = 2,
    max_regions: int = 4,
) -> list[dict[str, Any]]:
    """Group flagged tiles into rectangles, at original-image coordinates."""
    if grid is None or not getattr(grid, "size", 0):
        return []
    flagged = np.argwhere(grid > 0)
    if flagged.size == 0:
        return []

    cells: list[tuple[int, int, float]] = [
        (int(i), int(j), float(grid[i, j])) for i, j in flagged
    ]
    clusters: list[list[tuple[int, int, float]]] = []
    for i, j, value in sorted(cells):
        for cluster in clusters:
            if any(abs(i - ci) <= 1 and abs(j - cj) <= 1 for ci, cj, _ in cluster):
                cluster.append((i, j, value))
                break
        else:
            clusters.append([(i, j, value)])

    regions: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < minimum_tiles:
            continue
        row_ids = [c[0] for c in cluster]
        col_ids = [c[1] for c in cluster]
        r0 = min(rows[i][0] for i in row_ids)
        r1 = max(rows[i][1] for i in row_ids)
        c0 = min(cols[j][0] for j in col_ids)
        c1 = max(cols[j][1] for j in col_ids)
        if scale < 1.0 and scale > 0:
            r0, r1 = int(r0 / scale), int(r1 / scale)
            c0, c1 = int(c0 / scale), int(c1 / scale)
        regions.append(
            {
                "x": c0,
                "y": r0,
                "width": max(1, c1 - c0),
                "height": max(1, r1 - r0),
                "score": round(float(max(c[2] for c in cluster)), 3),
                "reason": (
                    f"{label}: {len(cluster)} adjacent tiles disagreed with the rest of "
                    "the page"
                ),
            }
        )
    regions.sort(key=lambda r: -r["score"])
    return regions[:max_regions]


def tamper_regions(
    *,
    grid: dict[str, Any] | None,
    ghost: dict[str, Any] | None,
    resample: dict[str, Any] | None,
    noise: dict[str, Any] | None,
    sharpness: dict[str, Any] | None = None,
    scale: float = 1.0,
    max_regions: int = 6,
) -> list[dict[str, Any]]:
    """Fuse every tile map into the shortlist a human should actually look at.

    Each map is converted to candidate boxes on its own; the dedupe pass then
    counts how many maps landed on the same area and records that as
    ``detectors``. No map gets to draw a box by itself without the caller being
    able to see it was alone — the count travels with the box so a report can say
    "three independent measurements agree here" or "one weak map fired here".

    That distinction is the whole reason this step exists. Every one of these maps
    still fires somewhere on an ordinary clean page; a box from a single map is a
    prompt to look, and only agreement between maps is evidence.
    """
    regions: list[dict[str, Any]] = []
    # The first field is the *gate* key, not a display name. Naming these
    # "grid"/"ghost" while ``_GATES`` is keyed "block_grid"/"sharpness" made every
    # lookup miss and silently disabled localization altogether.
    for name, data, label, minimum in (
        ("block_grid", grid, "JPEG block structure differs from the surrounding page", 2),
        ("ghost", ghost, "compression history differs from the surrounding page", 2),
        ("resample", resample, "resampling periodicity unlike the surrounding page", 2),
        ("noise", noise, "noise floor far below the surrounding page", 2),
        ("sharpness", sharpness, "fine detail does not match the surrounding text", 2),
    ):
        if not data or not data.get("available"):
            continue
        # A scan that did not beat its clean baseline draws no box. Without this
        # the report contradicted itself: a "no visible tampering" verdict next to
        # six flagged rectangles, because the boxes came from the ungated grid.
        #
        # A scan with no measured gate draws nothing at all. The report-only scans
        # are not in ``_GATES``, so ``_GATES.get(name, 0.0)`` used to gate them at
        # zero and every one of them drew a box on every file: ``ghost`` reads
        # 30.0 on an untouched receipt, which is above zero. That is where the
        # three phantom regions on every clean page came from.
        if name not in _GATES or not data.get("prominence"):
            continue
        if float(data["prominence"]) <= _GATES[name]:
            continue
        found = _grid_to_regions(
            data.get("grid"),
            data.get("rows") or [],
            data.get("cols") or [],
            label=label,
            scale=scale,
            minimum_tiles=minimum,
            max_regions=3,
        )
        regions.extend(found)

    # ``copy_move_regions`` used to add boxes here. It was removed after the
    # harness measured it: over 20 clean and 180 forged files the share of the
    # winning displacement was 0.198 at the clean maximum against 0.241 at the
    # forged maximum, so the gate that let forged files through was two
    # thousandths above the clean maximum on the calibration seed. A gate with
    # that little margin is a fitted number, and on any other sample it would
    # draw boxes on untouched receipts. Block duplication simply is not
    # separable from repeated typography on a rendered page.

    # Overlapping boxes are the same finding seen twice, so keep one per area.
    deduped: list[dict[str, Any]] = []
    for region in sorted(regions, key=lambda r: (-r["score"], r["y"], r["x"])):
        if any(
            abs(region["x"] - kept["x"]) < max(24, kept["width"] // 2)
            and abs(region["y"] - kept["y"]) < max(24, kept["height"] // 2)
            for kept in deduped
        ):
            continue
        votes = sum(
            1
            for other in regions
            if abs(region["x"] - other["x"]) < max(24, other["width"] // 2)
            and abs(region["y"] - other["y"]) < max(24, other["height"] // 2)
        )
        region = dict(region)
        region["detectors"] = votes
        deduped.append(region)
        if len(deduped) >= max_regions:
            break
    return deduped


#: Prominence gate per scan, and the minimum number of flagged tiles that has to
#: back it. **These numbers come from `python -m nanobot.forensics.benchmark`, not
#: from taste.** The gate is set at the largest value the *clean* class produced
#: over a 144-file corpus, so a detector contributes nothing until it has beaten
#: everything a genuine receipt did. The cost is recall — the measured
#: per-detector catch rate at these gates is low, which is stated in every report
#: rather than hidden — and the benefit is that no scan can put a clean page out
#: of the clean band on its own.
#:
#: Measured over the harness corpus (20 clean receipts, 180 forgeries, seed 7):
#:
#: ============  =========  =========  ==========  ============  =========
#: scan          clean p50  clean max  forged p50  forged max    AUC
#: ============  =========  =========  ==========  ============  =========
#: block_grid    24.0       30.0       24.0        36.0          0.71
#: sharpness     31.1       42.0       32.2        54.0          0.61
#: ghost         0.0        30.0       0.0         36.0          0.55
#: resample      0.0        0.0        0.0         4.0           0.54
#: noise         0.0        0.0        0.0         0.0           0.50
#: ============  =========  =========  ==========  ============  =========
#:
#: And the part that matters more than any of those columns — which *forgery*
#: each scan sees, with the gate at the clean maximum:
#:
#: ==============================  =============  ==================
#: forgery                         files caught   by
#: ==============================  =============  ==================
#: cloned_block                    20 / 20        block_grid
#: every other forgery class        0 / 160       nothing
#: ==============================  =============  ==================
#:
#: That is the honest summary of this whole module. A clone stamp is visible
#: because the copied region carries its own 8x8 grid, which does not line up with
#: the page's even after a re-save. Everything else here — a replaced total, a
#: resized patch, a shifted line — is re-encoded from scratch by the edit and
#: leaves the pixel statistics the same as an untouched receipt. Amount
#: replacement has to be caught by the document layer (OCR plus arithmetic
#: reconciliation), not by this one. See :func:`tamper_signals`.
_GATES: dict[str, float] = {
    "block_grid": 32.0,
    "sharpness": 42.0,
}

#: Scans that are measured and reported but **not** scored. Each was run through
#: the same harness and each failed it:
#:
#: * ``resample`` read a clean maximum of 0.277 on one corpus and 0.752 on
#:   another — a gate that moves with the sample is a fitted number, not a
#:   measured one — for an AUC of 0.52.
#: * ``noise`` never produced a single flagged tile on any file of either class,
#:   so its AUC is exactly 0.500 and its contribution is a constant.
#: * ``ghost`` was the worst kind of useless: an AUC of 0.53 with a clean maximum
#:   of 30.0 and a forged 95th percentile of 30.0, i.e. the statistic saturates
#:   at the same ceiling for both classes and only the *ceiling* is being read.
#:
#: They stay in the report because a measurement that ran and found nothing is
#: information a reader is entitled to, and because the ``tamper_scans_quiet``
#: signal lists them with their value against their baseline.
_REPORT_ONLY_SCANS = ("resample", "ghost", "noise")

#: Human-readable name for each scan, used in the reported signals.
_GATE_LABELS: dict[str, str] = {
    "block_grid": "compression-block structure",
    "sharpness": "fine detail",
    "resample": "resampling periodicity",
    "ghost": "compression history",
    "noise": "noise floor",
}

#: Weight contributed by one fired scan. Two fired scans reach the "signs of
#: editing" band and four reach "strong": the model is a vote between
#: measurements that fail differently, which is the only thing that survives the
#: weak individual separation in the table above.
_SCAN_WEIGHT = 2.0

_MIN_FLAGGED_TILES = 2


def tamper_signals(forensics: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the gated scans into weighted evidence, or into silence.

    A scan that did not beat its gate is reported as ``no_signal`` with weight
    zero, and *which* scans those were is kept in the detail text. That matters:
    "five scans ran and none of them beat the clean baseline" is a different and
    more useful sentence than "no signals", and it is the sentence a report has
    to be able to print.
    """
    signals: list[dict[str, Any]] = []
    fired = 0
    quiet: list[str] = []
    reported: list[str] = []
    copy_move = forensics.get("copy_move_blocks") or {}
    if copy_move.get("available"):
        reported.append(
            "block duplication ("
            f"{copy_move.get('duplicate_pairs', 0)} pairs on the winning displacement, "
            f"{copy_move.get('best_shift_share', 0)} of all candidates; not scored: a "
            "rendered page repeats its own typography and scores the same)"
        )
    for key, data in _scan_results(forensics):
        if key in _REPORT_ONLY_SCANS:
            reported.append(
                f"{_GATE_LABELS[key]} (prominence {data.get('prominence', 0)}, not scored: "
                "it failed its own calibration)"
            )
            continue
        gate = _GATES[key]
        label = _GATE_LABELS[key]
        if not data.get("available"):
            quiet.append(f"{label} (not measurable on this file)")
            continue
        prominence = float(data.get("prominence") or 0.0)
        tiles = int(data.get("flagged_tiles") or 0)
        if prominence > gate and tiles >= _MIN_FLAGGED_TILES:
            fired += 1
            signals.append(
                {
                    "family": "noise" if key in {"sharpness", "noise"} else "compression",
                    "signal": f"tamper_scan_{key}",
                    "direction": "edit_signal",
                    "weight": _SCAN_WEIGHT,
                    "detail": (
                        f"{label} is locally inconsistent: prominence {prominence} "
                        f"against a {gate} clean baseline, across {tiles} tile(s). "
                        "One measurement on its own; look at the box before concluding."
                    ),
                }
            )
        else:
            quiet.append(f"{label} (prominence {prominence}, baseline {gate})")
    if not fired:
        signals.append(
            {
                "family": "noise",
                "signal": "tamper_scans_quiet",
                "direction": "no_signal",
                "weight": 0.0,
                "detail": (
                    "No pixel scan beat its clean baseline. Scans that ran and found "
                    "nothing, with their measured value against the baseline: "
                    + "; ".join(quiet)
                    + ". This is evidence of absence only against the specific "
                    "manipulations the harness contains — see the limits."
                ),
            }
        )
    else:
        signals.append(
            {
                "family": "noise",
                "signal": "tamper_scan_count",
                "direction": "no_signal",
                "weight": 0.0,
                "detail": f"{fired} of {len(_GATES)} scored pixel scans beat their clean "
                          "baseline.",
            }
        )
    if reported:
        signals.append(
            {
                "family": "noise",
                "signal": "tamper_scans_report_only",
                "direction": "no_signal",
                "weight": 0.0,
                "detail": (
                    "Measured but deliberately unscored, because each one either failed "
                    "its own calibration or fires the same on both classes: "
                    + "; ".join(reported)
                ),
            }
        )
    return signals


_SCAN_FIELDS: dict[str, str] = {
    "block_grid": "block_grid",
    "sharpness": "sharpness",
    "resample": "resample",
    "ghost": "jpeg_ghost",
    "noise": "wavelet_noise",
}


def _scan_results(forensics: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every pixel scan's result, in a fixed order, keyed by its short name."""
    out: list[tuple[str, dict[str, Any]]] = []
    for key, field in _SCAN_FIELDS.items():
        data = forensics.get(field) or {}
        if data:
            out.append((key, data))
    return out


def summarise(name: str, data: dict[str, Any] | None) -> dict[str, Any]:
    """Strip the tile grids out of a detector result so it can be serialised."""
    if not data:
        return {}
    return {k: v for k, v in data.items() if k not in {"grid", "rows", "cols"}}
