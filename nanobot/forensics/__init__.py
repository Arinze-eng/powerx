"""Image and document forensics: is this file edited, and when was it taken?

Built for receipts and scanned documents, but nothing here is receipt-specific
except the layout and arithmetic checks. Everything runs on Pillow, NumPy and the
``tesseract`` binary — no model download, no network, no GPU — so it works inside
the agent process and inside an execution sandbox alike.

Read :mod:`nanobot.forensics.verdict` before trusting any of it. The package is
designed to hand back *weighted evidence and an explicit list of its own limits*,
because a confident wrong answer about a receipt is worse than no answer.
"""

from __future__ import annotations

from nanobot.forensics.document_forensics import (
    analyse_document,
    crop_region,
    ela_image,
    heatmap_overlay,
    ocr_available,
    reconcile,
)
from nanobot.forensics.image_forensics import (
    ImageForensics,
    Region,
    analyse_image,
    provenance_check,
)
from nanobot.forensics.sightova import (
    SightovaClient,
    resolve_api_key,
    run_detections,
)
from nanobot.forensics.verdict import collect_signals, render_report, score

__all__ = [
    "ImageForensics",
    "Region",
    "SightovaClient",
    "analyse_document",
    "analyse_image",
    "collect_signals",
    "crop_region",
    "ela_image",
    "heatmap_overlay",
    "ocr_available",
    "provenance_check",
    "reconcile",
    "render_report",
    "resolve_api_key",
    "run_detections",
    "score",
]
