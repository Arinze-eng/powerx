"""The tamper detectors and the harness that gates them.

Two properties carry this module and both are tested here:

1. A clean page produces no accusation. The gates are set at the largest value a
   clean class produced, so a regression that lowers one silently turns every
   genuine receipt into a suspect.
2. Every demoted check stays demoted. ``line_spacing``, ``font_geometry``, the
   duplication counts and the report-only scans were each measured firing on
   untouched files at the same rate as on edited ones; they are here as
   measurements, and the score must ignore them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.forensics import tamper  # noqa: E402
from nanobot.forensics.document_forensics import _REPORT_ONLY_FINDINGS  # noqa: E402
from nanobot.forensics.verdict import collect_signals, score  # noqa: E402


# --------------------------------------------------------------- local z score


def test_local_z_floors_the_noise_estimate_on_a_flat_page():
    """A blank page has a near-zero local MAD; dividing by it reported z=746.

    The floor is what makes the maps comparable between a blank page and a
    text-dense one, so it has to survive.
    """
    flat = np.full((64, 64), 128.0)
    flat[30:34, 30:34] = 200.0  # a small bright patch on nothing
    z = tamper._local_z(flat, radius=2)
    assert np.isfinite(z).all()
    assert float(np.max(np.abs(z))) <= tamper._MAX_Z


def test_local_z_is_capped():
    rng = np.random.default_rng(3)
    page = (rng.normal(128, 4, (96, 96))).astype(np.float32)
    page[40:48, 40:48] += 120.0
    z = tamper._local_z(page, radius=2)
    assert float(np.max(z)) == pytest.approx(tamper._MAX_Z)


def test_local_z_is_local_and_peaks_on_a_small_patch():
    """The score is against each cell's own neighbourhood, not the whole frame.

    A flat page is all zeros, and a patch small relative to its neighbourhood is
    where the statistic peaks. A page whose *halves* differ uniformly is not a
    finding: the robust local median absorbs a step that large, which is the
    property that stops ordinary shaded blocks and text density from scoring.
    """
    flat = np.full((160, 160), 200.0, dtype=np.float32)
    assert float(np.max(np.abs(tamper._local_z(flat, radius=2)))) == 0.0

    patched = flat.copy()
    patched[70:78, 70:78] = 60.0
    z = tamper._local_z(patched, radius=2)
    peak = np.unravel_index(int(np.argmax(np.abs(z))), z.shape)
    assert 68 <= peak[0] <= 80 and 68 <= peak[1] <= 80, peak
    assert float(z[80, 10]) == 0.0

    # Two halves at different brightness: the local median absorbs the step, so
    # neither interior is accused.
    split = np.full((160, 160), 220.0, dtype=np.float32)
    split[:, 80:] = 90.0
    split_z = tamper._local_z(split, radius=2)
    assert abs(float(split_z[80, 20])) < 2.0
    assert abs(float(split_z[80, 140])) < 2.0


# ------------------------------------------------------------ cluster strength


def test_cluster_strength_prefers_a_cluster_over_a_lone_spike():
    """Once z is capped, one extreme tile ranks the same as nine.

    A patch covers a neighbourhood, so the densest 3x3 window is the statistic;
    max was measured making clean and forged pages rank identically.
    """
    spike = np.zeros((7, 7), dtype=np.float32)
    spike[3, 3] = 15.0
    cluster = np.zeros((7, 7), dtype=np.float32)
    cluster[2:5, 2:5] = 6.0
    assert tamper._cluster_strength(cluster) > tamper._cluster_strength(spike)


def test_cluster_strength_is_zero_without_a_signal():
    assert tamper._cluster_strength(np.zeros((5, 5), dtype=np.float32)) == 0.0


# ------------------------------------------------------------------ haar bands


def test_haar_bands_agree_in_shape():
    """The texture reference and the noise estimate must be the same grid.

    They were not, once: texture was read at full resolution while sigma came
    from the half-resolution detail band, and the two could not be broadcast.
    """
    rng = np.random.default_rng(5)
    page = rng.normal(128, 6, (61, 45)).astype(np.float32)
    approx, detail = tamper._haar_bands(page)
    assert approx.shape == detail.shape
    assert approx.shape[0] <= page.shape[0]
    assert approx.shape[1] <= page.shape[1]


# --------------------------------------------------------------- copy-move gate


def test_blank_page_has_no_duplicate_blocks():
    """Texture is judged on AC coefficients only.

    Including DC made every blank block look textured and produced 6180 "copies"
    on a clean receipt, all of them row-to-row collisions between empty areas.
    """
    gray = np.full((320, 320), 250.0, dtype=np.float32)
    result = tamper.block_copy_move(gray)
    assert result.get("duplicate_pairs", 0) == 0


def test_duplicate_regions_draws_nothing_ungated():
    """The DCT matcher's own gate must hold, because it is measured at chance.

    Clean maximum 0.198 against a forged maximum of 0.241: no usable margin, so
    the answer for an ordinary page is no boxes.
    """
    assert tamper.copy_move_regions({"duplicate_pairs": 3, "best_shift_share": 0.9}) == []
    assert tamper.copy_move_regions(
        {"duplicate_pairs": 40, "best_shift_share": 0.05, "examples": []}
    ) == []


# ----------------------------------------------------------------- tamper regions


def test_ungated_scan_draws_no_region():
    """A scan with no measured gate must draw nothing.

    The report-only scans are absent from ``_GATES``, so gating them at a default
    of zero let them box every file: ``ghost`` reads 30.0 on an untouched receipt,
    which is above zero, and every clean page came back with three regions.
    """
    grid = _grid_payload(30.0)
    assert tamper.tamper_regions(
        grid=None, ghost=grid, resample=None, noise=None, sharpness=None
    ) == []


def _grid_payload(prominence: float) -> dict:
    """A flagged tile map with the ``(start, end)`` bounds the fusion expects."""
    return {
        "available": True,
        "prominence": prominence,
        "flagged_tiles": 36,
        "grid": np.ones((6, 6), dtype=np.float32),
        "rows": [(i * 32, i * 32 + 32) for i in range(6)],
        "cols": [(i * 32, i * 32 + 32) for i in range(6)],
    }


def test_scan_over_its_gate_draws_a_region():
    grid = _grid_payload(tamper._GATES["block_grid"] + 6.0)
    regions = tamper.tamper_regions(
        grid=grid, ghost=None, resample=None, noise=None, sharpness=None
    )
    assert regions
    assert regions[0]["detectors"] >= 1


def test_gates_only_cover_scored_scans():
    for name in tamper._REPORT_ONLY_SCANS:
        assert name not in tamper._GATES


# ----------------------------------------------------------------- tamper signals


def _forensics(**scans) -> dict:
    return dict(scans)


def test_quiet_scans_are_reported_at_zero_weight():
    signals = tamper.tamper_signals(
        _forensics(
            block_grid={"available": True, "prominence": 1.0, "flagged_tiles": 0},
            sharpness={"available": True, "prominence": 2.0, "flagged_tiles": 0},
            resample={"available": True, "prominence": 0.0, "flagged_tiles": 0},
        )
    )
    quiet = [s for s in signals if s["signal"] == "tamper_scans_quiet"]
    assert quiet and quiet[0]["weight"] == 0.0
    assert quiet[0]["direction"] == "no_signal"
    assert "baseline" in quiet[0]["detail"]
    assert not [s for s in signals if s["direction"] == "edit_signal"]


def test_report_only_scans_are_named_but_never_scored():
    signals = tamper.tamper_signals(
        _forensics(
            block_grid={"available": True, "prominence": 1.0, "flagged_tiles": 0},
            jpeg_ghost={"available": True, "prominence": 30.0, "flagged_tiles": 9},
            wavelet_noise={"available": True, "prominence": 0.0, "flagged_tiles": 0},
            resample={"available": True, "prominence": 9.0, "flagged_tiles": 4},
        )
    )
    scored = [s for s in signals if s["direction"] == "edit_signal"]
    assert scored == []
    report_only = [s for s in signals if s["signal"] == "tamper_scans_report_only"]
    assert report_only and report_only[0]["weight"] == 0.0
    assert "compression history" in report_only[0]["detail"]


def test_a_scan_over_its_gate_becomes_an_edit_signal():
    signals = tamper.tamper_signals(
        _forensics(
            block_grid={
                "available": True,
                "prominence": tamper._GATES["block_grid"] + 4.0,
                "flagged_tiles": 4,
            }
        )
    )
    fired = [s for s in signals if s["direction"] == "edit_signal"]
    assert len(fired) == 1
    assert fired[0]["weight"] > 0


def test_summarise_drops_the_tile_grids():
    stripped = tamper.summarise(
        "block_grid",
        {"prominence": 12.0, "grid": np.zeros((4, 4)), "rows": [0], "cols": [0]},
    )
    assert stripped == {"prominence": 12.0}


# --------------------------------------------- demoted checks stay demoted


def test_duplication_is_reported_but_never_scored():
    """The gradient-hash and DCT counts both measured at chance."""
    signals = collect_signals(
        {
            "copy_move": {"duplicate_pairs": 230},
            "copy_move_blocks": {"duplicate_pairs": 115},
        },
        None,
    )
    duplication = [s for s in signals if s["signal"] == "repeated_content_blocks"]
    assert duplication and duplication[0]["weight"] == 0.0
    assert duplication[0]["direction"] == "no_signal"
    assert "not scored" in duplication[0]["detail"]


def test_demoted_document_findings_do_not_move_the_band():
    document = {
        "findings": [
            {
                "signal": "line_spacing",
                "detail": "one gap is off",
                "weight": 0.0,
                "direction": "no_signal",
            }
        ]
    }
    verdict = score({}, document)
    assert verdict["band"] == "no_visible_tampering"
    assert verdict["score"] == 0.0


def test_arithmetic_finding_does_move_the_band():
    """The one content check that measured 100% with no false positives."""
    document = {
        "findings": [
            {"signal": "arithmetic", "detail": "total does not add up", "weight": 4.0}
        ]
    }
    verdict = score({}, document)
    assert verdict["band"] != "no_visible_tampering"
    assert verdict["score"] >= 4.0


def test_report_only_findings_are_the_measured_ones():
    assert set(_REPORT_ONLY_FINDINGS) == {"font_geometry", "line_spacing"}


# ----------------------------------------------------------------- the harness


def test_auc_is_a_half_when_the_classes_are_identical():
    from nanobot.forensics.benchmark import _auc

    assert _auc([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(0.5)


def test_auc_is_one_when_every_forgery_outranks_every_clean_file():
    from nanobot.forensics.benchmark import _auc

    assert _auc([1.0, 2.0], [5.0, 6.0]) == 1.0


def test_benchmark_measures_a_tiny_corpus(tmp_path: Path):
    """The harness has to run end to end: it is what gates every threshold here."""
    from nanobot.forensics.benchmark import AMOUNT_FORGERIES, _font_path, build_corpus, evaluate

    if _font_path() is None:
        pytest.skip("no TrueType font installed")

    samples = build_corpus(tmp_path, count=2, seed=1)
    assert len(samples) == 2 * (1 + len(AMOUNT_FORGERIES) + 6)
    assert {s.label for s in samples} == {"clean", "forged"}

    report = evaluate(samples)
    assert report["clean_total"] == 2
    assert report["failures"] == []
    names = {row["detector"] for row in report["detectors"]}
    assert {"block_grid_prom", "sharpness_prom", "copy_move"} <= names
    for row in report["detectors"]:
        assert -0.01 <= row["auc"] <= 1.01
