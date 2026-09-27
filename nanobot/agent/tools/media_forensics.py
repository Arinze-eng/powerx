"""Agent tool: decide whether an image or document has been edited, and when it was taken.

Use when the user hands over a receipt, a screenshot of a payment, an ID, a
contract scan, or any picture whose authenticity matters. The tool reports
weighted evidence plus its own limits; it never returns "genuine", because
nothing observable in a file proves that.

No model weights, no network, no GPU: Pillow + NumPy, and the ``tesseract``
binary when text checks are wanted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext

_ACTIONS = ("analyze", "timestamps", "ela", "localize", "compare", "timeline")

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "analyze: full forensic report (start here). "
                "timestamps: when the image was captured, plus device/GPS/software. "
                "ela: write an amplified error-level map and return its path. "
                "localize: write a heatmap of flagged regions and return their boxes. "
                "compare: diff two images and point at the changed regions. "
                "timeline: order several files by capture time and flag the odd ones out."
            ),
        },
        "path": {
            "type": "string",
            "description": "Image file to analyse. Absolute, or relative to the workspace.",
        },
        "other_path": {
            "type": "string",
            "description": (
                "Second image, for action='compare'. Use it to check a 'before' against an "
                "'after', or a claimed original against the document in hand."
            ),
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Two or more files, for action='timeline'.",
        },
        "document": {
            "type": "boolean",
            "description": (
                "Also run the receipt/document checks (OCR text lines, font and spacing "
                "geometry, arithmetic of total vs subtotal+tax, duplicate reference "
                "numbers). Default true for analyze."
            ),
        },
        "expected_amount": {
            "type": "string",
            "description": (
                "The amount the issuer's own record shows. This is the one input that can "
                "actually settle whether a receipt is real, so supply it whenever the user "
                "has it (a bank statement line, a gateway dashboard)."
            ),
        },
        "expected_date": {
            "type": "string",
            "description": "The date the issuer's record shows, e.g. 2026-09-27.",
        },
        "expected_reference": {
            "type": "string",
            "description": "The transaction reference / order id the issuer's record shows.",
        },
        "output_dir": {
            "type": "string",
            "description": "Where artifacts are written. Default: forensics/ in the workspace.",
        },
        "json_output": {
            "type": "boolean",
            "description": "Return the raw structured result instead of the markdown report.",
        },
    },
    "required": ["action"],
}


@tool_parameters(_SCHEMA)
class MediaForensicsTool(Tool):
    """Detect editing in images and documents, and read their capture timestamps."""

    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        return "media_forensics"

    @property
    def description(self) -> str:
        return (
            "Forensic analysis of an image or document: has it been edited, and when was it "
            "taken? Handles a fake or altered receipt, an edited screenshot of a payment, a "
            "tampered ID or scan, and a photo whose date the user wants. Reads EXIF/XMP "
            "capture time, device, GPS and editing software; runs error-level analysis, "
            "noise-floor consistency, JPEG quantisation-table and repeated-block checks; "
            "and for receipts also OCRs the text to check font and line-spacing geometry, "
            "duplicate reference numbers, and whether total = subtotal + tax. "
            "Pass expected_amount (and expected_date or expected_reference) from the "
            "issuer's OWN record whenever the user has it — reconciling against that is the "
            "only check that can actually settle authenticity. "
            "It never reports 'genuine': it returns weighted signals, a band, and the limits "
            "of those signals. Say so when relaying a result, and never present a clean "
            "report as proof. Use action='timestamps' for just the capture date."
        )

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    @property
    def read_only(self) -> bool:
        """Writes only the artifacts the caller asks for, under its own output dir."""
        return False

    def _workspace(self) -> Path:
        try:
            from nanobot.agent.tools.context import current_request_context

            ctx = current_request_context()
            if ctx is not None:
                for attr in ("workspace", "workspace_dir", "cwd"):
                    value = getattr(ctx, attr, None)
                    if value:
                        return Path(value)
                metadata = getattr(ctx, "metadata", None) or {}
                for key in ("workspace", "workspace_dir", "cwd"):
                    if metadata.get(key):
                        return Path(metadata[key])
        except Exception:
            pass
        return Path.cwd()

    def _resolve(self, raw: str, workspace: Path) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve()
        root = workspace.resolve()
        if root not in resolved.parents and resolved != root:
            raise ValueError(
                f"{raw!r} is outside the workspace. Forensic analysis only reads files "
                "inside the workspace; copy the file in first."
            )
        if not resolved.exists():
            raise ValueError(f"{raw!r} does not exist")
        if not resolved.is_file():
            raise ValueError(f"{raw!r} is not a file")
        return resolved

    async def execute(self, **kwargs: Any) -> Any:
        try:
            return self._run(kwargs)
        except Exception as exc:
            logger.exception("media_forensics tool failed")
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    # -- actions ---------------------------------------------------------------

    def _run(self, kwargs: dict[str, Any]) -> ToolResult:
        from nanobot.forensics import (
            analyse_document,
            analyse_image,
            render_report,
            score,
        )
        from nanobot.forensics.document_forensics import crop_region, ela_image, heatmap_overlay

        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return ToolResult.error(
                f"Unknown action {action!r}. Valid actions: {', '.join(_ACTIONS)}"
            )

        workspace = self._workspace()
        out_dir = Path(kwargs.get("output_dir") or (workspace / "forensics"))
        if not out_dir.is_absolute():
            out_dir = workspace / out_dir

        doc_enabled = kwargs.get("document")
        if doc_enabled is None:
            doc_enabled = action == "analyze"

        if action == "timeline":
            return self._timeline(kwargs, workspace)

        raw_path = str(kwargs.get("path") or "").strip()
        if not raw_path:
            return ToolResult.error("action='%s' needs a path" % action)
        path = self._resolve(raw_path, workspace)

        if action == "compare":
            return self._compare(kwargs, workspace, path)

        forensics = analyse_image(path).as_dict()

        if action == "timestamps":
            return self._timestamps(path, forensics)

        artifacts: dict[str, str] = {}
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = path.stem

        if action == "ela":
            target = out_dir / f"{stem}-ela.png"
            ela_image(path, target)
            return ToolResult(
                f"Error-level map written to `{target}`.\n"
                f"Max per-pixel error {forensics['ela'].get('max_abs_diff')}, mean "
                f"{forensics['ela'].get('mean_abs_diff')}. Bright areas differ most from a "
                "re-encode, which is where pasted content shows up — but normal text edges "
                "also differ, so this image is for looking at, not for measuring.\n"
            )

        if action == "localize":
            regions = forensics.get("regions") or []
            ela_target = out_dir / f"{stem}-ela.png"
            try:
                ela_image(path, ela_target)
                artifacts["ela"] = str(ela_target)
            except Exception as exc:
                logger.warning("ELA artifact failed: %s", exc)
            if not regions:
                note = (
                    "No repeated-content blocks passed the gate, so no boxes are flagged.\n\n"
                    "Worth being straight about the limit: on a text-heavy image, automatic "
                    "localisation of a pasted patch does not work with these methods. "
                    "Error-level analysis was measured here and a page of ordinary text "
                    "produces more error than a spliced-in patch does, so any threshold that "
                    "fires on the paste also fires on the text. What is left is the amplified "
                    f"error-level image at `{artifacts.get('ela')}` for you to look at, and "
                    "action='compare' when a second version of the file exists — a diff is "
                    "exact and is the reliable answer. Do not tell the user a clean ELA map "
                    "means the image is unedited.\n"
                )
                return ToolResult(note)
            overlay = out_dir / f"{stem}-regions.png"
            heatmap_overlay(path, regions, overlay)
            artifacts["heatmap"] = str(overlay)
            for index, region in enumerate(regions, start=1):
                crop = out_dir / f"{stem}-region{index}.png"
                crop_region(path, region, crop)
                artifacts[f"region{index}"] = str(crop)
            body = "\n".join(
                f"{i}. x={r['x']} y={r['y']} {r['width']}x{r['height']} "
                f"(score {r['score']}) — {r['reason']}"
                for i, r in enumerate(regions, start=1)
            )
            return ToolResult(
                f"Flagged regions (look at these yourself before concluding anything):\n{body}\n\n"
                + "\n".join(f"- {name}: `{p}`" for name, p in artifacts.items())
                + "\n"
            )

        document = None
        if doc_enabled:
            document = analyse_document(
                path,
                expected_amount=kwargs.get("expected_amount"),
                expected_date=kwargs.get("expected_date"),
                expected_reference=kwargs.get("expected_reference"),
            )

        verdict = score(forensics, document)

        ela_target = out_dir / f"{stem}-ela.png"
        try:
            ela_image(path, ela_target)
            artifacts["ela"] = str(ela_target)
        except Exception as exc:
            logger.warning("ELA artifact failed: %s", exc)
        regions = forensics.get("regions") or []
        if regions:
            overlay = out_dir / f"{stem}-regions.png"
            try:
                heatmap_overlay(path, regions, overlay)
                artifacts["regions_heatmap"] = str(overlay)
            except Exception as exc:
                logger.warning("region overlay failed: %s", exc)

        if kwargs.get("json_output"):
            payload = {
                "path": str(path),
                "forensics": {k: v for k, v in forensics.items() if k != "ela"},
                "document": document,
                "verdict": verdict,
                "artifacts": artifacts,
            }
            payload["forensics"]["ela"] = {
                k: v for k, v in (forensics.get("ela") or {}).items()
                if k != "normalised_display"
            }
            return ToolResult(json.dumps(payload, indent=2, default=str))

        report = render_report(
            path=str(path),
            forensics=forensics,
            document=document,
            verdict=verdict,
            artifacts=artifacts,
        )
        return ToolResult(report)

    def _timestamps(self, path: Path, forensics: dict[str, Any]) -> ToolResult:
        metadata = forensics.get("metadata") or {}
        lines = [f"# Capture time — {path}", ""]
        if metadata.get("captured_at"):
            lines.append(f"- **Taken: {metadata['captured_at']}** "
                         f"(from `{metadata.get('captured_at_source')}`)")
        else:
            lines.append("- **No capture timestamp in the file.**")
            lines.append(
                "  That is common and expected: screenshots, chat apps and most receipt "
                "exports strip EXIF. It means the file cannot tell you when it was taken."
            )
        if metadata.get("exif_write_after_capture"):
            lines.append(f"- File last written (EXIF DateTime): "
                         f"{metadata['exif_write_after_capture']} — after the capture, so "
                         "the file was rewritten at some point.")
        if metadata.get("file_modified"):
            lines.append(
                f"- Filesystem mtime: {metadata['file_modified']} — any copy, download or "
                "upload rewrites this, so it is the weakest date available and is not the "
                "capture time."
            )
        fields = metadata.get("exif_fields") or {}
        device = " ".join(str(fields.get(k, "")) for k in ("make", "model")).strip()
        lines.append(f"- Device: {device or 'not recorded'}")
        lines.append(f"- Software: {metadata.get('software') or 'not recorded'}")
        if metadata.get("gps"):
            lines.append(f"- GPS at capture: {metadata['gps']['label']}")
        else:
            lines.append("- GPS: not recorded")
        if metadata.get("password_note"):
            lines.append(f"- {metadata['password_note']}")
        lines.append("")
        lines.append(
            "Timestamps are written by whoever saved the file and can be set to anything. "
            "Treat a date as evidence only when it comes from the issuer's record or a "
            "signed credential, not from a camera tag alone."
        )
        lines.append("")
        return ToolResult("\n".join(lines))

    def _compare(self, kwargs: dict[str, Any], workspace: Path, path: Path) -> ToolResult:
        from nanobot.forensics.document_forensics import heatmap_overlay

        other_raw = str(kwargs.get("other_path") or "").strip()
        if not other_raw:
            return ToolResult.error("action='compare' needs both path and other_path")
        other = self._resolve(other_raw, workspace)

        from PIL import Image, ImageChops

        import numpy as np

        with Image.open(path) as first, Image.open(other) as second:
            first.load()
            second.load()
            size_note = ""
            if first.size != second.size:
                size_note = (
                    f"Different dimensions ({first.size} vs {second.size}); the second was "
                    "scaled to the first before diffing, so the diff is approximate.\n"
                )
                second = second.resize(first.size)
            a = first.convert("RGB")
            b = second.convert("RGB")
            diff = ImageChops.difference(a, b)
            arr = np.asarray(diff).max(axis=2).astype(np.float32)

        changed = float(np.mean(arr > 12))
        bbox = None
        if changed > 0:
            mask = arr > 12
            ys, xs = np.nonzero(mask)
            bbox = {
                "x": int(xs.min()), "y": int(ys.min()),
                "width": int(xs.max() - xs.min()), "height": int(ys.max() - ys.min()),
            }

        out_dir = Path(kwargs.get("output_dir") or (workspace / "forensics"))
        if not out_dir.is_absolute():
            out_dir = workspace / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        diff_path = out_dir / f"{path.stem}-vs-{other.stem}-diff.png"
        diff.save(diff_path)

        lines = [f"# Diff — {path.name} vs {other.name}", ""]
        if size_note:
            lines.append(size_note)
        lines.append(f"- Pixels differing by more than a hair: **{changed:.2%}** of the frame")
        if bbox:
            lines.append(f"- Everything that changed falls inside x={bbox['x']} y={bbox['y']} "
                         f"{bbox['width']}x{bbox['height']}")
        lines.append(f"- Diff image: `{diff_path}`")
        lines.append("")
        lines.append(
            "A diff tells you *that* two files differ, never *which* one is the original. "
            "If the two files come from different sources — a download and a phone photo, "
            "say — scaling, colour profile and re-encoding differences swamp any real edit."
        )
        lines.append("")
        return ToolResult("\n".join(lines))

    def _timeline(self, kwargs: dict[str, Any], workspace: Path) -> ToolResult:
        from nanobot.forensics import analyse_image

        raw_paths = kwargs.get("paths") or []
        if not isinstance(raw_paths, list) or len(raw_paths) < 2:
            return ToolResult.error("action='timeline' needs at least two paths")
        rows: list[dict[str, Any]] = []
        for raw in raw_paths:
            try:
                candidate = self._resolve(str(raw), workspace)
            except ValueError as exc:
                rows.append({"path": str(raw), "error": str(exc)})
                continue
            forensics = analyse_image(candidate, with_provenance=False).as_dict()
            metadata = forensics.get("metadata") or {}
            rows.append(
                {
                    "path": candidate.name,
                    "captured_at": metadata.get("captured_at"),
                    "source": metadata.get("captured_at_source"),
                    "software": metadata.get("software"),
                    "has_exif": metadata.get("has_exif"),
                }
            )
        known = [r for r in rows if r.get("captured_at")]
        known.sort(key=lambda r: str(r["captured_at"]))
        lines = ["# Timeline", ""]
        for row in known:
            lines.append(
                f"- {row['captured_at']}  {row['path']}  "
                f"(source: {row['source']}; software: {row['software'] or 'not recorded'})"
            )
        missing = [r for r in rows if not r.get("captured_at")]
        for row in missing:
            note = row.get("error") or "no capture timestamp in the file"
            lines.append(f"- (undated)  {row.get('path')}  — {note}")
        lines.append("")
        if missing:
            lines.append(
                f"{len(missing)} file(s) carry no capture time, which is normal for exports "
                "and screenshots. They cannot be placed on this timeline."
            )
        lines.append(
            "Ordering files by their own tags is only as trustworthy as the tags. To place a "
            "document in time, use the issuer's record."
        )
        lines.append("")
        return ToolResult("\n".join(lines))
