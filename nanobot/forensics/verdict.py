"""Weigh the forensic signals into a banded verdict — and say what it cannot mean.

The bands deliberately stop short of "genuine". Nothing observable in a file
proves an image was not fabricated, and a false *genuine* verdict on a receipt is
the expensive failure: it is what gets a fraudulent claim paid. So the strongest
positive thing this module will ever print is *no visible tampering*, and only a
signed C2PA manifest or a mismatch against the issuer's own record is allowed to
move the answer further than that.
"""

from __future__ import annotations

from typing import Any

#: Score bands. Order matters; the first threshold a score reaches wins.
_BANDS: tuple[tuple[float, str, str], ...] = (
    (7.0, "strong_signs_of_editing", "Multiple independent signals point at editing."),
    (3.0, "signs_of_editing", "Several signals point at editing or re-save."),
    (0.001, "weak_signals", "Some signals fired, each weak on its own."),
    (0.0, "no_visible_tampering", "No signal this analysis can see fired."),
)

#: Families that count as independent evidence, for a confidence estimate.
_FAMILIES = {
    "provenance",
    "metadata",
    "compression",
    "noise",
    "duplication",
    "layout",
    "arithmetic",
    "reconciliation",
}

#: A recent high-quality re-save wipes most compression history, so the pixel
#: signals deserve less weight when the file says that is what happened.
_REENCODED_DAMPING = 0.55


def _add(
    signals: list[dict[str, Any]],
    family: str,
    name: str,
    direction: str,
    weight: float,
    detail: str,
) -> None:
    signals.append(
        {
            "family": family,
            "signal": name,
            "direction": direction,
            "weight": round(weight, 2),
            "detail": detail,
        }
    )


def collect_signals(forensics: dict[str, Any], document: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Turn the raw measurements into a flat, weighted list of evidence."""
    signals: list[dict[str, Any]] = []
    metadata = forensics.get("metadata") or {}
    ela = forensics.get("ela") or {}
    noise = forensics.get("noise") or {}
    jpeg = forensics.get("jpeg") or {}
    copy_move = forensics.get("copy_move") or {}
    provenance = forensics.get("provenance") or {}
    regions = forensics.get("regions") or []

    # ---- provenance: the only decisive positive signal in this file ----------
    if provenance.get("manifest_present"):
        state = str(provenance.get("validation_state") or "")
        if "invalid" in state.lower() or "failure" in state.lower():
            _add(
                signals, "provenance", "c2pa_manifest_invalid", "edit_signal", 6.0,
                f"A C2PA manifest is present but did not validate ({state or 'unknown'}). "
                "A broken content credential is a serious signal.",
            )
        else:
            _add(
                signals, "provenance", "c2pa_manifest_valid", "authenticity_signal", -9.0,
                "A C2PA content credential is present and validates, so the signing app "
                "has attested to the asset's history.",
            )
    elif provenance.get("checked"):
        _add(
            signals, "provenance", "c2pa_absent", "no_signal", 0.0,
            "No C2PA manifest. Almost no camera or receipt app writes one yet, so this "
            "distinguishes nothing.",
        )

    # ---- container and metadata ---------------------------------------------
    if metadata.get("editor_software_detected"):
        _add(
            signals, "metadata", "editor_software_named", "edit_signal", 1.2,
            f"Metadata names editing software: {metadata.get('software')!r}. Records that "
            "an editor wrote the file, not what it changed.",
        )
    if metadata.get("edit_history"):
        _add(
            signals, "metadata", "xmp_edit_history", "edit_signal", 1.0,
            "XMP carries an edit history: " + ", ".join(metadata["edit_history"][:6]),
        )
    if metadata.get("exif_write_after_capture"):
        _add(
            signals, "metadata", "exif_written_after_capture", "edit_signal", 0.8,
            f"EXIF DateTime ({metadata['exif_write_after_capture']}) is later than "
            f"DateTimeOriginal ({metadata.get('captured_at')}), so the file was rewritten "
            "after the shot.",
        )
    if metadata.get("metadata_stripped"):
        _add(
            signals, "metadata", "metadata_absent", "edit_signal", 0.6,
            "A JPEG/TIFF with no EXIF at all: a screenshot, a chat-app re-save, or a "
            "deliberate scrub. Indistinguishable from each other.",
        )

    # ---- compression ---------------------------------------------------------
    quality = jpeg.get("quality")
    damp = 1.0
    if quality is not None and quality >= 95:
        damp = _REENCODED_DAMPING
        _add(
            signals, "compression", "high_quality_resave", "weak_signal", 0.7,
            f"Quantisation tables match a JPEG quality near {quality}. A recent re-save "
            "at high quality leaves little history, so the pixel signals below are damped.",
        )
    if jpeg.get("is_jpeg") and jpeg.get("standard_luma_table") is False:
        _add(
            signals, "compression", "non_standard_quantisation_table", "edit_signal",
            0.9 * damp,
            "The luminance quantisation table is not a textbook IJG table, which happens "
            "with a re-encode through a different encoder or a non-standard tool chain.",
        )
    if not jpeg.get("is_jpeg"):
        _add(
            signals, "compression", "not_jpeg", "no_signal", 0.0,
            f"Format is {jpeg.get('format') or forensics.get('format')}: error-level "
            "analysis carries much less meaning outside JPEG, so those weights are halved.",
        )

    # ---- ELA -----------------------------------------------------------------
    # Reported, never scored. Measured on a 12-line receipt: tile-mean error peaks
    # at 1.43 clean versus 1.61 with a patch spliced in, so any threshold that
    # fires on a paste also fires on ordinary text. It is an artifact to look at,
    # not a number to threshold.
    peak = ela.get("max_abs_diff") or 0.0
    mean = ela.get("mean_abs_diff") or 0.0
    if ela.get("available") and peak > 0:
        _add(
            signals, "compression", "ela_baseline", "no_signal", 0.0,
            f"Whole-frame error level: max {peak}, mean {mean}. Recorded for comparison "
            "and for the ELA image; not treated as a finding, because text edges produce "
            "more error than a pasted patch does.",
        )

    # ---- noise ---------------------------------------------------------------
    # Also reported only: the spread tracks how much of the page is text versus
    # blank, which is a property of the document, not of any edit.
    fraction = noise.get("outlier_fraction")
    if fraction is not None:
        _add(
            signals, "noise", "noise_floor_spread", "no_signal", 0.0,
            f"{fraction:.1%} of tiles have a noise/texture floor far from the rest of the "
            "frame. This mostly measures how much of the page is blank versus text.",
        )

    # ---- duplication ---------------------------------------------------------
    # Regions are only produced when the strict duplication gate in
    # image_forensics passes, so their presence is the signal.
    dupes = copy_move.get("duplicate_pairs") or 0
    if regions:
        _add(
            signals, "duplication", "repeated_content_blocks", "edit_signal",
            min(3.0, 0.5 * dupes) * damp,
            f"{dupes} pairs of non-uniform blocks repeat across the frame, past the gate "
            "for what repeated UI rows or a watermark would produce. Worth looking at the "
            "flagged boxes.",
        )

    # ---- document layer ------------------------------------------------------
    for finding in (document or {}).get("findings", []):
        _add(
            signals,
            "layout" if finding.get("signal") != "arithmetic" else "arithmetic",
            finding.get("signal", "document_finding"),
            "edit_signal",
            float(finding.get("weight") or 1.0),
            str(finding.get("detail") or ""),
        )

    reconciliation = (document or {}).get("reconciliation") or {}
    if reconciliation.get("mismatches"):
        detail = "; ".join(
            f"{m.get('field')}: {m.get('detail') or m}" for m in reconciliation["mismatches"]
        )
        _add(
            signals, "reconciliation", "issuer_record_mismatch", "edit_signal", 8.0,
            "The document contradicts the issuer's own record — " + detail,
        )
    elif reconciliation.get("matches"):
        _add(
            signals, "reconciliation", "issuer_record_matches", "authenticity_signal", -2.0,
            "Every supplied field matches the document: "
            + ", ".join(f"{m.get('field')}" for m in reconciliation["matches"]),
        )

    return signals


def score(forensics: dict[str, Any], document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate signals into a band, a confidence, and an explicit list of limits."""
    signals = collect_signals(forensics, document)
    positive = sum(s["weight"] for s in signals if s["direction"] == "edit_signal")
    negative = sum(s["weight"] for s in signals if s["direction"] == "authenticity_signal")
    total = positive + negative

    reconciliation = (document or {}).get("reconciliation") or {}
    provenance = forensics.get("provenance") or {}

    band = "no_visible_tampering"
    reason = "No signal this analysis can see fired."
    for threshold, name, blurb in _BANDS:
        if total >= threshold:
            band, reason = name, blurb
            break

    # A mismatch against the issuer's record outranks everything pixels can say,
    # and a validated credential outranks every edit signal.
    decisive = None
    if reconciliation.get("mismatches"):
        band = "contradicts_issuer_record"
        decisive = "issuer_record_mismatch"
        reason = (
            "The document disagrees with the issuer's own record. That is a substantive "
            "inconsistency, independent of how the image looks."
        )
    elif provenance.get("manifest_present") and negative <= -9.0 and positive < 3.0:
        band = "credential_verified"
        decisive = "c2pa_manifest_valid"
        reason = (
            "A signed C2PA credential validates and no edit signal fired. This is the "
            "only positive finding in the package that is worth relying on."
        )

    families = {s["family"] for s in signals if s["direction"] == "edit_signal" and s["weight"] > 0}
    if decisive:
        confidence = "high"
        confidence_basis = f"decided by {decisive}, which does not depend on pixel analysis"
    elif not families:
        confidence = "low"
        confidence_basis = "nothing fired; absence of signals is weak evidence in either direction"
    elif len(families) >= 3:
        confidence = "moderate"
        confidence_basis = f"{len(families)} independent signal families agree"
    else:
        confidence = "low"
        confidence_basis = f"only {len(families)} signal family fired"

    limits = [
        "This cannot prove an image or document is genuine. Nothing observable in a file "
        "can, and a wrong 'genuine' on a receipt is the expensive mistake.",
        "Error-level, noise and compression signals are destroyed by one re-encode, a "
        "screenshot, or a print-and-scan round trip. Their absence proves nothing.",
        "A generated receipt rendered convincingly has no editing history to find at all. "
        "Layout and arithmetic checks are what catch that class, and they are heuristics.",
        "Metadata is written by whoever authored the file and can be forged as easily as "
        "the pixels.",
        "The only checks strong enough to act on are a validated C2PA credential and a "
        "mismatch against the issuer's own record.",
    ]
    if not reconciliation.get("checked"):
        limits.append(
            "No expected amount, date or reference was supplied, so the document was never "
            "reconciled against the issuer. That is the check that would actually settle it."
        )
    if not provenance.get("available"):
        limits.append(
            "The c2pa package is not installed, so signed provenance was not read. "
            "Install c2pa to enable the one reliable positive signal."
        )

    return {
        "band": band,
        "reason": reason,
        "score": round(total, 2),
        "positive_score": round(positive, 2),
        "negative_score": round(negative, 2),
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "decisive_signal": decisive,
        "families": sorted(families),
        "signals": signals,
        "cannot_prove_genuine": True,
        "limits": limits,
    }


def render_report(
    *,
    path: str,
    forensics: dict[str, Any],
    document: dict[str, Any] | None,
    verdict: dict[str, Any],
    artifacts: dict[str, str] | None = None,
) -> str:
    """Render the whole analysis as the markdown block the model reads back."""
    lines: list[str] = []
    add = lines.append
    metadata = forensics.get("metadata") or {}
    jpeg = forensics.get("jpeg") or {}
    ela = forensics.get("ela") or {}
    noise = forensics.get("noise") or {}
    copy_move = forensics.get("copy_move") or {}
    provenance = forensics.get("provenance") or {}

    add(f"# Forensic analysis — {path}")
    add("")
    add(f"**Verdict band: `{verdict['band']}`** (confidence: {verdict['confidence']})")
    add("")
    add(f"{verdict['reason']}")
    add("")
    add(f"- Score: {verdict['score']} (edit signals +{verdict['positive_score']}, "
        f"authenticity signals {verdict['negative_score']})")
    add(f"- Confidence basis: {verdict['confidence_basis']}")
    add("- **This output cannot prove anything genuine.** See *What this does not say*.")
    add("")

    add("## When the image says it was taken")
    add("")
    if metadata.get("captured_at"):
        add(f"- **Captured: {metadata['captured_at']}** "
            f"(source: `{metadata.get('captured_at_source')}`)")
    else:
        add("- **No capture timestamp found in the file.**")
    if metadata.get("exif_write_after_capture"):
        add(f"- File last written (EXIF DateTime): {metadata['exif_write_after_capture']}")
    if metadata.get("file_modified"):
        add(f"- Filesystem modified time: {metadata['file_modified']} "
            "(rewritten by any copy, download or upload — not evidence)")
    add(f"- Device: {metadata.get('exif_fields', {}).get('make', '?')} "
        f"{metadata.get('exif_fields', {}).get('model', '')}".rstrip())
    add(f"- Software tag: {metadata.get('software') or 'absent'}")
    if metadata.get("gps"):
        add(f"- GPS: {metadata['gps']['label']}")
    if metadata.get("xmp"):
        add(f"- XMP: {metadata['xmp']}")
    if metadata.get("edit_history"):
        add(f"- XMP edit history: {', '.join(metadata['edit_history'][:8])}")
    add("")

    add("## Image")
    add("")
    add(f"- {forensics.get('width')}x{forensics.get('height')} px, "
        f"{forensics.get('megapixels')} MP, {forensics.get('format')}, mode {forensics.get('mode')}")
    add(f"- JPEG quality estimate: {jpeg.get('quality')}; "
        f"standard luma table: {jpeg.get('standard_luma_table')}; "
        f"progressive: {jpeg.get('progressive')}; ICC profile: {jpeg.get('icc_profile')}")
    add("")

    add("## Signals")
    add("")
    add("| family | signal | direction | weight | detail |")
    add("|---|---|---|---|---|")
    for signal in verdict["signals"]:
        add(f"| {signal['family']} | {signal['signal']} | {signal['direction']} | "
            f"{signal['weight']} | {str(signal['detail']).replace('|', '/')} |")
    add("")

    add("## Measurements")
    add("")
    add(f"- ELA: max {ela.get('max_abs_diff')}, mean {ela.get('mean_abs_diff')}, "
        f"p99 {ela.get('p99_abs_diff')} (re-encode at q{ela.get('quality_used', 90)})")
    add(f"- Noise tiles: CV {noise.get('coefficient_of_variation')}, "
        f"outliers {noise.get('outlier_fraction')}")
    add(f"- Repeated content blocks: {copy_move.get('duplicate_pairs')} pairs, "
        f"uniform tiles {copy_move.get('uniform_tile_fraction')}")
    add(f"- C2PA: present={provenance.get('manifest_present')}, "
        f"state={provenance.get('validation_state')}")
    regions = forensics.get("regions") or []
    if regions:
        add("")
        add("Regions flagged for a human to look at:")
        add("")
        for index, region in enumerate(regions, start=1):
            add(f"{index}. x={region['x']} y={region['y']} "
                f"{region['width']}x{region['height']} (score {region['score']}) — "
                f"{region['reason']}")
    add("")

    if document:
        add("## Document / receipt checks")
        add("")
        if document.get("text_available"):
            add(f"- OCR ({document.get('ocr_engine')}): "
                f"{document.get('characters_read')} characters, "
                f"{len(document.get('text_lines') or [])} lines")
            arithmetic = document.get("arithmetic") or {}
            add(f"- Total read: {arithmetic.get('total')}; subtotal {arithmetic.get('subtotal')}; "
                f"tax {arithmetic.get('tax')}; consistent: {arithmetic.get('consistent')}")
            fonts = document.get("fonts") or {}
            add(f"- Font geometry: median line height {fonts.get('median_height')}, "
                f"{fonts.get('outlier_count')} outlier(s)")
            spacing = document.get("spacing") or {}
            add(f"- Line spacing: typical {spacing.get('typical_gap')}px, "
                f"{spacing.get('irregular_count')} irregular gap(s)")
            add(f"- Duplicate long numbers: {len(document.get('duplicates') or [])}")
        else:
            add(f"- Text checks did not run: {document.get('ocr_error')}")
        reconciliation = document.get("reconciliation") or {}
        add(f"- Reconciliation: asked={reconciliation.get('asked')}, "
            f"mismatches={len(reconciliation.get('mismatches') or [])}, "
            f"matches={len(reconciliation.get('matches') or [])}")
        if reconciliation.get("note"):
            add(f"- {reconciliation['note']}")
        add("")

    if artifacts:
        add("## Files written")
        add("")
        for name, written in artifacts.items():
            add(f"- {name}: `{written}`")
        add("")

    add("## What this does not say")
    add("")
    for limit in verdict["limits"]:
        add(f"- {limit}")
    return "\n".join(lines) + "\n"
