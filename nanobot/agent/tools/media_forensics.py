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
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.config_base import Base


class MediaForensicsToolConfig(Base):
    """Configuration for media forensics and its hosted detection API.

    The API is **on by default when a key resolves**: it is the only check in
    the package that can see a generated file, so silently skipping it would
    leave the tool's weakest blind spot in place. Set ``detection_api=false``
    to stay offline; the local engine then runs alone and the report says so.
    """

    #: Where to ask. Sightova's host by default; point at the RapidAPI gateway or
    #: a mirror with the same ``/api/v1/detect/*`` contract.
    base_url: str = "https://sightova.com"
    #: One or more bearer keys. A list rotates: when a key is out of plan or quota
    #: it is retired and the next serves the request, so one dry key does not
    #: force the local fallback. A single string is accepted too, split on commas
    #: or newlines. Leave empty to read SIGHTOVA_API_KEYS / SIGHTOVA_API_KEY.
    api_keys: list[str] = Field(default_factory=list, repr=False)
    #: Kept for a single-key deployment and folded into ``api_keys`` on load.
    api_key: str = Field(default="", repr=False)
    #: "round_robin" spreads requests evenly and keeps the order reportable;
    #: "random" spreads the starting point when key budgets are unequal.
    key_strategy: str = "round_robin"
    #: Master switch. False forces the local engine; the report then states that
    #: the generated class was not checked.
    detection_api: bool = True
    #: Endpoints to consult, in order. "ai" is the generator detector and is
    #: always worth one request; "document" is the tampering detector, which some
    #: plans do not cover — it is attempted and its refusal recorded.
    detection_kinds: list[str] = Field(default_factory=lambda: ["ai", "document"])
    timeout_seconds: float = 60.0

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
        "sandbox": {
            "type": "string",
            "enum": ["auto", "off", "require"],
            "description": (
                "Where to read the pixels. 'auto' (default) uses the execution sandbox when one "
                "is configured and falls back to this host otherwise; 'off' always analyses "
                "here; 'require' refuses rather than analysing here. The verdict, the wording "
                "and the artifacts are produced the same way either way — only the pixel and "
                "OCR work moves."
            ),
        },
        "detection": {
            "type": "string",
            "enum": ["auto", "off"],
            "description": (
                "Whether to consult the hosted detection API. 'auto' (default) asks it first "
                "for action='analyze' when a key is configured — it is the only check here "
                "that can see a *generated* image or document, which has no editing history "
                "for the local pixel scans to find. 'off' skips it and uses the local engine "
                "alone, and the report then says the generated class was not checked."
            ),
        },
        "detection_kinds": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Which API detectors to run, in order. Defaults to the configured set "
                "('ai' for generated images, 'document' for tampered documents). Each is "
                "one API scan; drop one to save budget when only that class matters."
            ),
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
            "report as proof. Use action='timestamps' for just the capture date. "
            "For action='analyze' a hosted detection API is consulted FIRST when a key is "
            "configured: it is the only check here that can see a generated (synthetic) "
            "image or a convincingly re-rendered document, which leaves no editing history "
            "for the local pixel scans to find. If the API cannot answer — no key, no "
            "network, or a plan that does not cover the endpoint — the local engine runs as "
            "the fallback and the report states the blind spot explicitly. Several API keys "
            "may be configured; they are rotated, and a key that is out of plan or quota is "
            "retired in favour of the next rather than dropping the run to the fallback."
        )

    def __init__(self, ctx: ToolContext | None = None) -> None:
        # Retained so the sandbox tool can be resolved at execute() time: the
        # registry is not populated while the tool classes are being loaded, so a
        # lookup in ``create`` would always come back empty.
        self._ctx: ToolContext | None = ctx
        #: Set when a sandbox run was attempted, so the report can say where the
        #: pixels were read. None means this host read them.
        self._sandbox_note: str | None = None
        #: The hosted detection reading, set by execute() before _run(). None
        #: means the API was not in play, which the report states as a limit.
        self._detection: dict[str, Any] | None = None
        #: Sightova client settings, from the tool config or its defaults.
        self._api_keys: list[str] = []
        self._api_base_url: str = "https://sightova.com"
        self._api_enabled: bool = True
        self._api_kinds: tuple[str, ...] = ("ai", "document")
        self._api_timeout: float = 60.0
        self._api_strategy: str = "round_robin"
        #: Built lazily on the first analyze call, then held so key retirements
        #: survive across files in the same session.
        self._rotator: Any | None = None

    @classmethod
    def create(cls, ctx: ToolContext) -> "MediaForensicsTool":
        """Carry the tool context, and the detection-API settings, into the tool."""
        from nanobot.forensics.sightova import _split_keys, resolve_api_keys

        cfg = getattr(ctx.config, "media_forensics", None)
        instance = cls(ctx)
        if cfg is None:
            instance._api_keys = resolve_api_keys(None)
            return instance

        # ``api_keys`` is the list form; ``api_key`` is the single-key legacy
        # field. Both are folded together, list first, so an operator who added a
        # second key without clearing the first gets both rather than losing one.
        configured = _split_keys(getattr(cfg, "api_keys", None)) + _split_keys(
            getattr(cfg, "api_key", None)
        )
        instance._api_base_url = str(getattr(cfg, "base_url", "") or "https://sightova.com")
        instance._api_enabled = bool(getattr(cfg, "detection_api", True))
        instance._api_keys = resolve_api_keys(configured)
        kinds = tuple(
            str(k).strip().lower()
            for k in (getattr(cfg, "detection_kinds", None) or [])
            if str(k).strip()
        )
        if kinds:
            instance._api_kinds = kinds
        instance._api_timeout = float(getattr(cfg, "timeout_seconds", 60.0) or 60.0)
        instance._api_strategy = str(getattr(cfg, "key_strategy", "round_robin") or "round_robin")
        return instance

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
            action = str(kwargs.get("action") or "").strip().lower()
            self._detection = None
            # The hosted detection API is tried FIRST, for the one action where a
            # verdict is produced. It is the only check in the package that can
            # see a generated file, and the local engine's fallback cannot. A
            # refusal or a network failure is recorded, never fatal: the run
            # continues on the local checks and the report says so.
            if action == "analyze":
                await self._detect(kwargs)
            precomputed = await self._prefetch(kwargs, action)
            if precomputed is False:
                # The reason, when the relay learned one, is included: "no sandbox
                # could do it" is not actionable on its own, and the difference
                # between "none is configured" and "the one that is would not come
                # up" is the difference between changing a config and retrying.
                reason = f" {self._sandbox_note}." if self._sandbox_note else ""
                return ToolResult.error(
                    "sandbox='require' was asked for, but no execution sandbox could analyse "
                    f"these files.{reason} Nothing was analysed on this host, and nothing is "
                    "wrong with the file: either no sandbox is configured, or the sandbox could "
                    "not be provisioned. Retry with sandbox='auto' to run the analysis here."
                )
            result = self._run(kwargs, precomputed=precomputed or None)
            return self._with_sandbox_note(result, kwargs)
        except Exception as exc:
            logger.exception("media_forensics tool failed")
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    # -- hosted detection API --------------------------------------------------

    async def _detect(self, kwargs: dict[str, Any]) -> None:
        """Ask the hosted detection API about the file before the local checks.

        Best-effort and never raising: any failure leaves ``self._detection`` as
        a dict with ``available=False`` and the reason, so the verdict layer can
        name the blind spot instead of pretending the file was checked. The API
        result is what the report leads with; the local pixel work below still
        runs, because it measures different classes and the two are shown side by
        side rather than one replacing the other.
        """
        mode = str(kwargs.get("detection") or "auto").strip().lower()
        if mode in ("off", "false", "skip", "no"):
            self._detection = {
                "available": False,
                "detections": [],
                "unavailable": [{"kind": "all", "reason": "detection API skipped by request"}],
            }
            return

        raw_path = str(kwargs.get("path") or "").strip()
        if not raw_path:
            self._detection = None
            return
        try:
            path = self._resolve(raw_path, self._workspace())
        except ValueError:
            # A bad path is _run()'s error to report, with its own wording.
            self._detection = None
            return

        if not self._api_enabled:
            self._detection = {
                "available": False,
                "detections": [],
                "unavailable": [
                    {"kind": "all", "reason": "detection_api is disabled in the tool config"}
                ],
                "keys": {"total": len(self._api_keys), "available": 0, "strategy": self._api_strategy},
            }
            return

        from nanobot.forensics.sightova import KeyRotator, resolve_api_keys, run_detections

        keys = resolve_api_keys(self._api_keys)
        if not keys:
            self._detection = {
                "available": False,
                "detections": [],
                "unavailable": [{"kind": "all", "reason": "no Sightova API key is configured"}],
                "keys": {"total": 0, "available": 0, "strategy": self._api_strategy},
            }
            return

        kinds = self._api_kinds
        requested = kwargs.get("detection_kinds")
        if isinstance(requested, list) and requested:
            kinds = tuple(str(k).strip().lower() for k in requested if str(k).strip())

        # One rotator per tool instance, so a key retired while analysing one file
        # stays retired for the next file in the same session instead of being
        # re-asked and re-refused on every call.
        if self._rotator is None:
            self._rotator = KeyRotator(keys, strategy=self._api_strategy)

        self._detection = await run_detections(
            path,
            base_url=self._api_base_url or "https://sightova.com",
            kinds=kinds,
            timeout_seconds=self._api_timeout,
            rotator=self._rotator,
        )

    # -- sandbox relay ---------------------------------------------------------

    def _targets(self, kwargs: dict[str, Any], action: str, workspace: Path) -> list[Path]:
        """The files whose pixels this action needs, resolved and checked.

        Deliberately the same set the host path would read, so a sandbox run and a
        host run analyse the same files and nothing is silently skipped. ``compare``
        is absent because its diff is pure Pillow and cheap; ``timeline`` names
        several files and wants only their tags.
        """
        if action == "compare":
            return []
        raws: list[str] = []
        if action == "timeline":
            raws = [str(p) for p in (kwargs.get("paths") or []) if str(p).strip()]
        elif str(kwargs.get("path") or "").strip():
            raws = [str(kwargs["path"])]
        resolved: list[Path] = []
        for raw in raws:
            try:
                resolved.append(self._resolve(raw, workspace))
            except ValueError:
                # A bad path is the action's error to report, with its own wording,
                # and it must not become a sandbox failure. It is simply not shipped.
                continue
        return resolved

    async def _prefetch(
        self, kwargs: dict[str, Any], action: str
    ) -> dict[str, dict[str, Any]] | None | bool:
        """Analyse the targets in the sandbox and return ``{path: analysis}``.

        Returns ``None`` when the sandbox is not in play at all, and ``False`` only
        when ``sandbox='require'`` was asked for and no sandbox could serve it — the
        one case where the caller must refuse rather than quietly analyse here.
        """
        mode = str(kwargs.get("sandbox") or "auto").strip().lower()
        if mode == "off" or action not in _ACTIONS:
            return None
        if not self._ctx:
            return False if mode == "require" else None

        from nanobot.agent.tools.forensics_sandbox import ForensicsRelay, sandbox_tool

        sandbox = sandbox_tool(self._ctx)
        if sandbox is None:
            return False if mode == "require" else None

        workspace = self._workspace()
        targets = self._targets(kwargs, action, workspace)
        if not targets:
            return None
        # `timestamps` and `timeline` want the container tags only; `ela` and
        # `localize` are decisions about pixels. Neither needs the OCR layer, and OCR
        # is the slow half of a scan. `analyze` is the one action that defaults it on.
        wants_document = bool(kwargs.get("document")) or (
            action == "analyze" and kwargs.get("document") is not False
        )

        relay = ForensicsRelay(sandbox)
        try:
            payload = await relay.analyse(
                [(path.name, path) for path in targets],
                document=wants_document,
                expected_amount=kwargs.get("expected_amount"),
                expected_date=kwargs.get("expected_date"),
                expected_reference=kwargs.get("expected_reference"),
                with_provenance=action != "timeline",
            )
        except Exception as exc:  # noqa: BLE001
            # The sandbox is an optimisation, not a dependency. A sandbox that is
            # cold, wedged or offline must cost a little latency, never the answer:
            # the host path is still right here and still correct.
            logger.warning("media_forensics: sandbox analysis unavailable ({}), using host", exc)
            self._sandbox_note = f"sandbox unavailable ({exc})"
            return False if mode == "require" else None

        by_name = payload.get("files") or {}
        out: dict[str, dict[str, Any]] = {}
        for path in targets:
            entry = by_name.get(path.name)
            if not isinstance(entry, dict) or entry.get("error"):
                continue
            forensics = entry.get("forensics")
            if not isinstance(forensics, dict):
                continue
            out[str(path)] = {
                "forensics": forensics,
                "document": entry.get("document"),
                "seconds": entry.get("seconds"),
            }
        if not out:
            self._sandbox_note = "the sandbox returned no usable analysis"
            return False if mode == "require" else None
        self._sandbox_note = (
            f"pixels read in the sandbox ({payload.get('sandbox_id') or 'backend'}, "
            f"peak {payload.get('peak_rss_mb')} MB, {payload.get('sandbox_seconds')}s)"
        )
        return out

    def _with_sandbox_note(self, result: ToolResult, kwargs: dict[str, Any]) -> ToolResult:
        """Say where the pixels were read, when a sandbox read them.

        Appended to the rendered output rather than woven into the report, so that a
        sandbox run and a host run still say the SAME thing about the file: this line
        is provenance, not a finding, and it must not be readable as one. Skipped for
        JSON output, where the note travels as a field of its own, and skipped on an
        error, where the error already explains itself.
        """
        if not self._sandbox_note or result.is_error or kwargs.get("json_output"):
            return result
        return ToolResult(
            str(result).rstrip("\n")
            + f"\n\n_Where the pixels were read: {self._sandbox_note}._\n"
        )

    def _analysis(
        self,
        path: Path,
        precomputed: dict[str, dict[str, Any]] | None,
        *,
        with_provenance: bool = True,
    ) -> dict[str, Any]:
        """The raw analysis for one file — from the sandbox if it is there, else here."""
        if precomputed:
            entry = precomputed.get(str(path))
            if entry and isinstance(entry.get("forensics"), dict):
                return entry["forensics"]
        from nanobot.forensics import analyse_image

        return analyse_image(path, with_provenance=with_provenance).as_dict()

    def _document_analysis(
        self,
        path: Path,
        kwargs: dict[str, Any],
        precomputed: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """The OCR/layout layer for one file, from whichever machine read it.

        A sandbox that returned ``None`` for the text layer is reported as an absent
        layer rather than silently re-run here: re-running would double the OCR cost
        this whole path exists to avoid, and the report already says when the text
        layer is missing.
        """
        if precomputed:
            entry = precomputed.get(str(path))
            if entry:
                document = entry.get("document")
                return document if isinstance(document, dict) else None
        from nanobot.forensics import analyse_document

        return analyse_document(
            path,
            expected_amount=kwargs.get("expected_amount"),
            expected_date=kwargs.get("expected_date"),
            expected_reference=kwargs.get("expected_reference"),
        )

    # -- actions ---------------------------------------------------------------

    def _run(
        self, kwargs: dict[str, Any], precomputed: dict[str, dict[str, Any]] | None = None
    ) -> ToolResult:
        from nanobot.forensics import render_report, score
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
            return self._timeline(kwargs, workspace, precomputed)

        raw_path = str(kwargs.get("path") or "").strip()
        if not raw_path:
            return ToolResult.error("action='%s' needs a path" % action)
        path = self._resolve(raw_path, workspace)

        if action == "compare":
            return self._compare(kwargs, workspace, path)

        forensics = self._analysis(path, precomputed)

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
            document = self._document_analysis(path, kwargs, precomputed)

        verdict = score(forensics, document, self._detection)

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
                "detection": self._detection,
                "verdict": verdict,
                "artifacts": artifacts,
                "sandbox": self._sandbox_note,
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
            detection=self._detection,
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

    def _timeline(
        self,
        kwargs: dict[str, Any],
        workspace: Path,
        precomputed: dict[str, dict[str, Any]] | None = None,
    ) -> ToolResult:
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
            forensics = self._analysis(candidate, precomputed, with_provenance=False)
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
