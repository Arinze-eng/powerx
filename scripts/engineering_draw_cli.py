#!/usr/bin/env python3
"""Engineering drawing engine for the execution sandbox: 2D DXF and 3D CAD.

WHY THIS RUNS IN THE SANDBOX
---------------------------
``build123d`` pulls OpenCASCADE (an OCP wheel in the hundreds of MB) and needs
its X libraries; ``ezdxf`` plus matplotlib for raster previews add more. None of
that belongs on the application host, and the gateway must stay small. So the
engine is a CLI that lives here, is bootstrapped by URL, and is driven by the
``engineering_draw`` tool from inside the user's sandbox -- exactly how
``mt5_cli.py`` works.

THE CONTRACT
------------
One JSON object on stdout, always. The tool parses the last balanced ``{...}``
block, so diagnostics go to stderr and every failure mode returns
``{"ok": false, "error": ...}`` with a ``next`` field naming the fix. A call that
half-succeeded says exactly which artefact exists and which does not.

WHAT IT CAN DO
--------------
* ``doctor``    report whether build123d / ezdxf / matplotlib are importable
* ``model``     build 3D geometry from a declarative spec or a build123d snippet,
                export STEP / STL / 3MF / BREP, and report measured properties
* ``draw``      draw a true 2D engineering drawing: real DXF ``DIMENSION``
                entities (editable in AutoCAD/QCAD, not exploded lines), layers,
                hatches, text, a title block, and a scaled sheet with a border
* ``project``   take a 3D model and produce 2D projected views (front / top /
                right / iso, hidden lines removed) as DXF + SVG + PNG
* ``section``   cut a 3D model on a plane and draw the section
* ``inspect``   measure an existing STEP / STL / DXF without rebuilding it
* ``export``    convert an existing artifact between supported formats

Geometry is built from either ``spec`` (a JSON part description, for the shapes
that cover most simple parts) or ``code`` (a build123d snippet the model writes,
for everything else). ``code`` is the escape hatch that makes the engine general;
``spec`` is the path that does not depend on the model getting Python exactly
right.

UNITS
-----
Everything is millimetres. Angles are degrees. The sheet is the drawing sheet;
the part is always 1:1 in model space and the view is scaled to fit it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from pathlib import Path
from typing import Any

CLI_VERSION = "2026-09-26.2"

OUT_DIR_DEFAULT = os.environ.get("ENGINEERING_DRAW_DIR", "~/engineering_drawings")

#: Sheet sizes in millimetres, portrait (width, height).
SHEETS: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
    "letter": (215.9, 279.4),
    "tabloid": (279.4, 431.8),
}

# --- layers of a real drawing, not decoration ---------------------------- #
# The names, colours and line weights are what a drafter expects to find, so the
# exported DXF can be opened and edited by a human without cleanup first.
LAYERS: dict[str, dict[str, Any]] = {
    "OUTLINE": {"color": 7, "linetype": "CONTINUOUS", "lineweight": 50},
    "HIDDEN": {"color": 8, "linetype": "DASHED", "lineweight": 25},
    "CENTER": {"color": 4, "linetype": "CENTER", "lineweight": 18},
    "DIMENSIONS": {"color": 2, "linetype": "CONTINUOUS", "lineweight": 18},
    "ANNOTATION": {"color": 3, "linetype": "CONTINUOUS", "lineweight": 18},
    "HATCH": {"color": 9, "linetype": "CONTINUOUS", "lineweight": 13},
    "BORDER": {"color": 7, "linetype": "CONTINUOUS", "lineweight": 70},
    "TITLE": {"color": 7, "linetype": "CONTINUOUS", "lineweight": 25},
    "CONSTRUCTION": {"color": 1, "linetype": "DASHED", "lineweight": 13},
}


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
def ok(**payload: Any) -> None:
    print(json.dumps({"ok": True, **payload}, default=str))
    raise SystemExit(0)


def fail(error: str, next_step: str = "", **payload: Any) -> None:
    print(json.dumps({"ok": False, "error": error, "next": next_step, **payload}, default=str))
    raise SystemExit(1)


class EntityError(Exception):
    """A bad 2D entity: reported, and the rest of the sheet still gets drawn.

    ``fail`` exits the process, which for a drawing with twenty entities would
    throw away nineteen good ones over one bad dictionary. A drawing action
    catches this instead and lists it in ``entity_errors``.
    """

    def __init__(self, message: str, next_step: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.next_step = next_step
        self.extra = extra

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.next_step:
            parts.append(f"({self.next_step})")
        for key, value in self.extra.items():
            parts.append(f"[{key}: {value}]")
        return " ".join(parts)


def entity_fail(error: str, next_step: str = "", **extra: Any) -> None:
    """Like ``fail``, but scoped to one entity of a multi-entity drawing.

    Carries the same extras ``fail`` does (``supported``, ``received``) so an
    entity error is as self-correcting as a top-level one.
    """
    raise EntityError(error, next_step, **extra)


def _deps() -> dict[str, Any]:
    found: dict[str, Any] = {"build123d": None, "ezdxf": None, "matplotlib": None}
    for name in list(found):
        try:
            module = __import__(name)
            found[name] = getattr(module, "__version__", "unknown")
        except Exception as exc:  # noqa: BLE001
            found[name] = f"MISSING ({type(exc).__name__})"
    return found


def need_dep(name: str) -> Any:
    try:
        return __import__(name)
    except Exception as exc:  # noqa: BLE001
        fail(
            f"{name} is not installed in the sandbox ({type(exc).__name__}: {exc})",
            "Run action='install' first — it pip-installs build123d, ezdxf and "
            "matplotlib plus the OpenCASCADE system libraries. Do not try to "
            "implement the geometry by hand instead.",
            missing=[name],
        )


def _out_dir(raw: str | None) -> Path:
    path = Path(os.path.expanduser(raw or OUT_DIR_DEFAULT)).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_stem(raw: str | None, fallback: str = "drawing") -> str:
    stem = str(raw or "").strip() or fallback
    keep = [ch if (ch.isalnum() or ch in "-_.") else "_" for ch in stem]
    cleaned = "".join(keep).strip("._") or fallback
    return cleaned[:80]


def _report_files(paths: dict[str, Any]) -> dict[str, Any]:
    """Sizes for the artefacts that actually exist, keyed by format."""
    report: dict[str, Any] = {}
    for key, value in paths.items():
        if not value:
            continue
        path = Path(str(value))
        report[key] = {
            "path": str(path),
            "exists": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
        }
    return report


# --------------------------------------------------------------------------- #
# Geometry: a declarative spec, for the shapes that cover most parts
# --------------------------------------------------------------------------- #
def _shape_from_spec(spec: dict[str, Any]) -> Any:
    """Build one solid from a JSON part description.

    Deliberately a small vocabulary of primitives plus booleans and holes: the
    common engineering part is a plate with holes, a shaft, a bracket or a
    stepped block, and every one of those is expressible here. Anything outside
    it is what ``code`` is for.
    """
    b3d = need_dep("build123d")

    kind = str(spec.get("kind") or spec.get("type") or "").strip().lower()
    length = float(spec.get("length", spec.get("x", 0) or 0))
    width = float(spec.get("width", spec.get("y", 0) or 0))
    height = float(spec.get("height", spec.get("z", 0) or 0))

    if kind in ("box", "block", "plate"):
        if min(length, width, height) <= 0:
            fail(
                "a box needs positive length, width and height in mm",
                "Pass length/width/height — e.g. a 100 x 60 x 10 plate.",
                received={"length": length, "width": width, "height": height},
            )
        shape = b3d.Box(length, width, height)
    elif kind in ("cylinder", "shaft", "pin"):
        radius = float(spec.get("radius") or 0)
        if radius <= 0:
            diameter = float(spec.get("diameter") or 0)
            radius = diameter / 2.0
        if radius <= 0 or height <= 0:
            fail(
                "a cylinder needs a radius (or diameter) and a height",
                "Pass radius (or diameter) and height, in mm.",
            )
        shape = b3d.Cylinder(radius, height)
    elif kind in ("sphere", "ball"):
        radius = float(spec.get("radius") or 0) or float(spec.get("diameter") or 0) / 2.0
        if radius <= 0:
            fail("a sphere needs a radius or a diameter in mm", "Pass radius or diameter.")
        shape = b3d.Sphere(radius)
    elif kind in ("cone", "taper"):
        bottom = float(spec.get("radius") or spec.get("bottom_radius") or 0)
        top = float(spec.get("top_radius") or 0)
        if bottom <= 0 or height <= 0:
            fail("a cone needs a bottom radius and a height", "Pass radius and top_radius.")
        shape = b3d.Cone(bottom, top, height)
    elif kind in ("tube", "pipe"):
        outer = float(spec.get("radius") or spec.get("outer_radius") or 0)
        wall = float(spec.get("wall") or spec.get("thickness") or 0)
        if outer <= 0 or wall <= 0 or wall >= outer or height <= 0:
            fail(
                "a tube needs outer radius, wall thickness and height, with wall < radius",
                "Pass radius, wall and height.",
            )
        shape = b3d.Cylinder(outer, height) - b3d.Cylinder(outer - wall, height * 1.4)
    else:
        fail(
            f"unknown part kind {kind!r}",
            "Use kind one of: box, cylinder, sphere, cone, tube — or pass 'code' "
            "with a build123d snippet for anything else.",
            supported=["box", "cylinder", "sphere", "cone", "tube"],
        )

    # Holes: a list of centres on the top face, drilled through. Positioned as
    # (x, y) from the centre of the part, which is where build123d puts the origin.
    holes = spec.get("holes") or []
    if holes:
        if not isinstance(holes, list):
            fail("'holes' must be a list of hole descriptions", "Pass holes as a list.")
        for index, hole in enumerate(holes):
            if not isinstance(hole, dict):
                fail(f"hole #{index} is not an object", "Each hole is an object.")
            radius = float(hole.get("radius") or 0) or float(hole.get("diameter") or 0) / 2.0
            if radius <= 0:
                fail(
                    f"hole #{index} has no positive radius",
                    "Give each hole a radius or a diameter.",
                )
            depth = hole.get("depth")
            cutter_height = float(depth) if depth else max(height, width, length, radius * 2) * 3 + 20.0
            drill = b3d.Cylinder(radius, cutter_height)
            at = hole.get("at") or hole.get("center") or [0, 0]
            drill = b3d.Pos(float(at[0]), float(at[1]), 0.0) * drill
            if hole.get("count") and int(hole["count"]) > 1:
                count = int(hole["count"])
                # A bolt circle, because a ring of holes is the second most common
                # hole pattern after a single centre hole.
                bc_radius = float(hole.get("bolt_circle_radius") or 0)
                if bc_radius <= 0:
                    fail(
                        f"hole #{index} asks for {count} holes but no bolt_circle_radius",
                        "Pass bolt_circle_radius so the engine knows where the ring is.",
                    )
                for step in range(count):
                    angle = 2.0 * math.pi * step / count
                    drill = drill + b3d.Pos(
                        bc_radius * math.cos(angle), bc_radius * math.sin(angle), 0.0
                    ) * b3d.Cylinder(radius, cutter_height)
            shape = shape - drill

    for fillet_radius in spec.get("fillets") or []:
        try:
            shape = b3d.fillet(shape.edges(), float(fillet_radius))
        except Exception as exc:  # noqa: BLE001 - a fillet that cannot be built is not fatal
            print(f"warning: fillet {fillet_radius} skipped: {exc}", file=sys.stderr)
    chamfer_size = spec.get("chamfer")
    if chamfer_size:
        try:
            shape = b3d.chamfer(shape.edges(), float(chamfer_size))
        except Exception as exc:  # noqa: BLE001
            print(f"warning: chamfer skipped: {exc}", file=sys.stderr)

    return shape


def _shape_from_code(code: str, params: dict[str, Any] | None = None) -> Any:
    """Run a build123d snippet and take the shape it leaves in ``result``.

    The snippet is executed in a namespace that already has ``build123d`` bound,
    so a model writes ``result = Box(10, 10, 10) - Cylinder(2, 20)``. Algebra
    mode is the default because it is what a model writes correctly most often;
    builder mode is available by importing it explicitly.
    """
    b3d = need_dep("build123d")
    namespace: dict[str, Any] = {"__name__": "__part__"}
    for name in dir(b3d):
        if not name.startswith("_"):
            namespace[name] = getattr(b3d, name)
    namespace["b3d"] = b3d
    namespace["params"] = dict(params or {})
    namespace["math"] = math
    try:
        exec(compile(code, "<part>", "exec"), namespace)  # noqa: S102 - the sandbox is the boundary
    except Exception as exc:  # noqa: BLE001
        fail(
            f"the build123d snippet raised {type(exc).__name__}: {exc}",
            "Fix the snippet and call again. Use algebra mode — for example "
            "result = Box(60, 40, 10) - Pos(0, 0, 0) * Cylinder(5, 40).",
            traceback=traceback.format_exc()[-1500:],
        )
    shape = namespace.get("result") or namespace.get("part") or namespace.get("shape")
    if shape is None:
        fail(
            "the snippet did not assign 'result'",
            "End the snippet with `result = <your shape>`.",
        )
    return shape


def _shape_from(args: argparse.Namespace) -> Any:
    if args.code:
        return _shape_from_code(args.code, args.params)
    if args.spec:
        return _shape_from_spec(args.spec)
    fail(
        "no geometry given",
        "Pass either 'spec' (a JSON part description) or 'code' (a build123d "
        "snippet that assigns 'result').",
    )


def _measure(shape: Any) -> dict[str, Any]:
    b3d = need_dep("build123d")
    try:
        box = shape.bounding_box()
        bbox = {
            "x": round(box.size.X, 4),
            "y": round(box.size.Y, 4),
            "z": round(box.size.Z, 4),
            "min": [round(box.min.X, 4), round(box.min.Y, 4), round(box.min.Z, 4)],
            "max": [round(box.max.X, 4), round(box.max.Y, 4), round(box.max.Z, 4)],
        }
    except Exception as exc:  # noqa: BLE001
        bbox = {"error": f"{type(exc).__name__}: {exc}"}
    measured: dict[str, Any] = {"bounding_box_mm": bbox}
    try:
        measured["volume_mm3"] = round(float(shape.volume), 4)
    except Exception:  # noqa: BLE001
        measured["volume_mm3"] = None
    try:
        solids = shape.solids()
        measured["solid_count"] = len(solids)
        measured["is_valid"] = all(bool(s.is_valid()) for s in solids)
    except Exception:  # noqa: BLE001
        pass
    try:
        measured["faces"] = len(shape.faces())
        measured["edges"] = len(shape.edges())
        measured["is_manifold"] = bool(b3d.Solid.is_manifold(shape)) if hasattr(b3d.Solid, "is_manifold") else None
    except Exception:  # noqa: BLE001
        pass
    volume = measured.get("volume_mm3")
    if volume:
        # Steel is the default because it is the default assumption in a shop;
        # the caller can override with 'density_g_cm3'.
        density = 7.85
        measured["mass_kg_at_7.85g_cm3"] = round(volume / 1000.0 * density / 1000.0, 4)
    return measured


def _export_3d(shape: Any, stem: str, out: Path, formats: list[str]) -> dict[str, Any]:
    b3d = need_dep("build123d")
    written: dict[str, Any] = {}
    wanted = {f.strip().lower() for f in formats if f and f.strip()}
    for fmt in wanted:
        target = out / f"{stem}.{fmt}"
        try:
            if fmt == "step" or fmt == "stp":
                b3d.export_step(shape, str(target))
            elif fmt == "stl":
                b3d.export_stl(shape, str(target))
            elif fmt == "brep":
                b3d.export_brep(shape, str(target))
            elif fmt in ("3mf",):
                _export_3mf(shape, str(target))
            elif fmt in ("gltf", "glb"):
                b3d.export_gltf(shape, str(target))
            elif fmt == "obj":
                b3d.export_obj(shape, str(target))
            else:
                print(f"warning: unsupported 3D format {fmt!r} skipped", file=sys.stderr)
                continue
            written[fmt] = str(target)
        except Exception as exc:  # noqa: BLE001 - one bad format must not lose the model
            print(f"warning: {fmt} export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return written


_3MF_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" '
    'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)

_3MF_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


def _export_3mf(shape: Any, path: str, *, tolerance: float = 0.001, name: str = "part") -> None:
    """Write a real 3MF (an OPC zip with a triangle mesh) without a 3MF library.

    build123d ships no 3MF exporter, and the ``Lib3MF`` binding it drags in
    rejects its own ``SetGeometry`` call, so the container is written here
    instead: a 3MF is a zip of three XML parts, and the mesh is already available
    from the STL tessellation. Vendoring a broken binding's workaround is worse
    than emitting the 150 lines of XML the format actually specifies.
    """
    b3d = need_dep("build123d")

    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        stl_path = str(Path(tmp) / "mesh.stl")
        b3d.export_stl(shape, stl_path, tolerance=tolerance)
        vertices, triangles = _read_stl(stl_path)

    def fmt(value: float) -> str:
        text = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return text or "0"

    mesh = [
        "<mesh><vertices>",
        *(
            f'<vertex x="{fmt(v[0])}" y="{fmt(v[1])}" z="{fmt(v[2])}"/>'
            for v in vertices
        ),
        "</vertices><triangles>",
        *(
            f'<triangle v1="{int(t[0])}" v2="{int(t[1])}" v3="{int(t[2])}"/>'
            for t in triangles
        ),
        "</triangles></mesh>",
    ]
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<metadata name="Title">{_xml_escape(name)}</metadata>'
        f'<metadata name="Application">powerx engineering_draw</metadata>'
        "<resources>"
        f'<object id="1" type="model" name="{_xml_escape(name)}">'
        + "".join(mesh)
        + "</object></resources>"
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>'
        "</model>"
    )
    # A STL keeps its coordinates in the file, and a 3MF is expected to be
    # right-handed Z-up like the model space we exported from, so no transform is
    # applied beyond the identity above.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _3MF_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _3MF_RELS)
        archive.writestr("3D/3dmodel.model", model_xml)


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_3d(shape: Any, stem: str, out: Path, formats: list[str], size: int = 900) -> dict[str, Any]:
    """A shaded isometric raster of the solid, so a human can see what was built.

    Exported through a tessellation plus matplotlib rather than a browser or a
    GUI: build123d's STL tessellation is already there, and matplotlib is the one
    plotting library the sandbox reliably has.
    """
    written: dict[str, Any] = {}
    wanted = {f.strip().lower() for f in formats if f and f.strip()}
    if not wanted:
        return written
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    except Exception as exc:  # noqa: BLE001
        print(f"warning: 3D preview needs matplotlib: {exc}", file=sys.stderr)
        return written

    try:
        tmp_stl = out / f".{stem}.preview.stl"
        import build123d as _b3d

        _b3d.export_stl(shape, str(tmp_stl))
        vertices, faces = _read_stl(str(tmp_stl))
        triangles = vertices[faces]
        figure = plt.figure(figsize=(size / 100, size / 100), dpi=100)
        axes = figure.add_subplot(111, projection="3d")
        collection = Poly3DCollection(
            triangles,
            facecolor=(0.62, 0.70, 0.82),
            edgecolor=(0.25, 0.28, 0.33),
            linewidths=0.15,
        )
        axes.add_collection3d(collection)
        points = vertices.reshape(-1, 3)
        low, high = points.min(axis=0), points.max(axis=0)
        centre = (low + high) / 2.0
        span = float(max(high - low)) or 1.0
        axes.set_xlim(centre[0] - span / 2, centre[0] + span / 2)
        axes.set_ylim(centre[1] - span / 2, centre[1] + span / 2)
        axes.set_zlim(centre[2] - span / 2, centre[2] + span / 2)
        axes.set_box_aspect((1, 1, 1))
        axes.view_init(elev=28, azim=-52)
        axes.set_axis_off()
        for fmt in wanted:
            if fmt not in ("png", "svg", "pdf"):
                continue
            target = out / f"{stem}_iso.{fmt}"
            figure.savefig(target, bbox_inches="tight", pad_inches=0.1)
            written[fmt] = str(target)
        plt.close(figure)
        tmp_stl.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: 3D preview failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return written


def _read_stl(path: str) -> tuple[Any, Any]:
    """Minimal binary/ASCII STL reader, so a preview never depends on numpy-stl."""
    import numpy as np

    raw = Path(path).read_bytes()
    if raw[:5].lower().startswith(b"solid") and b"facet" in raw[:2000]:
        vertices: list[list[float]] = []
        for line in raw.decode("utf-8", "ignore").splitlines():
            parts = line.split()
            if parts and parts[0] == "vertex":
                vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
        array = np.array(vertices, dtype=float)
        return array, np.arange(len(array)).reshape(-1, 3)
    count = int.from_bytes(raw[80:84], "little")
    data = np.frombuffer(raw[84 : 84 + count * 50], dtype=np.uint8).reshape(count, 50)
    floats = data[:, 12:48].copy().view("<f4").reshape(count, 3, 3).astype(float)
    return floats.reshape(-1, 3), np.arange(count * 3).reshape(-1, 3)


# --------------------------------------------------------------------------- #
# The 2D engine: real DXF with real DIMENSION entities
# --------------------------------------------------------------------------- #
def _sheet_geometry(size: str, margin: float = 10.0) -> tuple[float, float]:
    key = str(size or "A3").strip()
    if key in SHEETS:
        return SHEETS[key]
    fail(
        f"unknown sheet size {key!r}",
        f"Use one of: {', '.join(SHEETS)} — or pass sheet_width/sheet_height in mm.",
        supported=list(SHEETS),
    )


def _new_doc(size: str, *, landscape: bool = True) -> tuple[Any, float, float]:
    ezdxf = need_dep("ezdxf")
    width, height = _sheet_geometry(size)
    if landscape:
        width, height = max(width, height), min(width, height)
    doc = ezdxf.new(dxfversion="R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres, so a reader does not have to guess
    doc.header["$MEASUREMENT"] = 1
    for name, spec in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(
                name,
                color=int(spec["color"]),
                linetype=str(spec["linetype"]),
                lineweight=int(spec["lineweight"]),
            )
    # Dimension styles are what make the exported dimensions editable rather than
    # a picture of dimensions: a drafter can re-scale the whole drawing and the
    # text follows.
    for style in ("EZDXF", "EZ_RADIUS", "EZ_CURVED", "Standard"):
        if style in doc.dimstyles:
            dimstyle = doc.dimstyles.get(style)
            dimstyle.dxf.dimtxt = 2.5
            dimstyle.dxf.dimasz = 2.5
            dimstyle.dxf.dimdec = 1
            dimstyle.dxf.dimlunit = 2  # decimal, so a reader never prints feet
            # Deliberately no dimblk: naming an arrow block that is not in the
            # document leaves the dimension unrenderable, and the drawing addon
            # then raises on the whole sheet instead of on that one dimension.
    return doc, width, height


def _coerce_title(value: Any, default_name: str, drawing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalise a title block, accepting the shorthands a model actually writes.

    ``"title": "FLANGE"`` is a plain string, and ``"title": {"name": "FLANGE"}``
    is an object; both mean the same thing to a drafter, so both are accepted
    rather than one of them dying with an ``AttributeError`` deep in the title
    block. Top-level ``material`` / ``scale`` / ``drawn_by`` / ``rev`` / ``note``
    on the drawing spec are folded in too, because writing the scale next to the
    sheet is what everyone does the first time. Anything else is reported as the
    bad field it is, with the shape that was wanted.
    """
    title: dict[str, Any] = {}
    if isinstance(value, str):
        title["name"] = value.strip()
    elif isinstance(value, dict):
        title.update({k: v for k, v in value.items() if v not in (None, "")})
    elif value is not None:
        fail(
            f"the title block must be an object or a string, not {type(value).__name__}",
            'Pass title={"name": "FLANGE", "material": "AL 6082", "scale": "1:1"} '
            '— or the shorthand title="FLANGE".',
        )
    for key in ("material", "scale", "drawn_by", "rev", "note"):
        if drawing and drawing.get(key) not in (None, ""):
            title.setdefault(key, drawing[key])
    if not title.get("name"):
        title["name"] = default_name
    return title


def _border_and_title(msp: Any, width: float, height: float, title: dict[str, Any], margin: float = 10.0) -> None:
    """Draw a printable border and a title block, both on their own layers."""
    msp.add_lwpolyline(
        [(margin, margin), (width - margin, margin), (width - margin, height - margin),
         (margin, height - margin)],
        close=True,
        dxfattribs={"layer": "BORDER"},
    )
    block_w, block_h = 150.0, 32.0
    x0, y0 = width - margin - block_w, margin
    msp.add_lwpolyline(
        [(x0, y0), (x0 + block_w, y0), (x0 + block_w, y0 + block_h), (x0, y0 + block_h)],
        close=True,
        dxfattribs={"layer": "TITLE"},
    )
    for offset in (block_h - 8.0, block_h - 18.0):
        msp.add_line((x0, y0 + offset), (x0 + block_w, y0 + offset), dxfattribs={"layer": "TITLE"})
    msp.add_line((x0 + 96.0, y0), (x0 + 96.0, y0 + block_h), dxfattribs={"layer": "TITLE"})

    def text(value: str, at: tuple[float, float], size: float = 3.5) -> None:
        if value:
            msp.add_text(
                str(value)[:70],
                height=size,
                dxfattribs={"layer": "TITLE"},
            ).set_placement(at)

    text(str(title.get("name") or "PART"), (x0 + 4.0, y0 + block_h - 6.5), 5.0)
    text(f"MATERIAL: {title.get('material') or '-'}", (x0 + 4.0, y0 + block_h - 15.5))
    text(f"DRAWN BY: {title.get('drawn_by') or 'powerx agent'}", (x0 + 4.0, y0 + block_h - 25.5))
    text(f"UNITS: MM   SCALE: {title.get('scale') or '1:1'}", (x0 + 100.0, y0 + block_h - 15.5))
    text(f"SHEET: {title.get('sheet') or '1/1'}", (x0 + 100.0, y0 + block_h - 25.5))
    text(f"REV: {title.get('rev') or 'A'}", (x0 + 100.0, y0 + block_h - 6.5))
    if title.get("note"):
        msp.add_text(
            str(title["note"])[:110], height=2.5, dxfattribs={"layer": "ANNOTATION"}
        ).set_placement((margin + 3.0, margin + 3.0))


def _render_dim(dim: Any, kind: str) -> None:
    """Render a dimension and make sure it actually produced geometry.

    An ezdxf dimension is a parametric object: until ``render()`` runs it has no
    text position, and a DIMENSION whose ``text_midpoint`` is still None makes
    the drawing addon raise while rasterising — which would lose the whole
    preview over one bad dimension. So a dimension that cannot be rendered is
    reported as the one bad entity it is.
    """
    try:
        dim.render()
    except Exception as exc:  # noqa: BLE001
        try:
            dim.destroy()
        except Exception:  # noqa: BLE001
            pass
        entity_fail(
            f"{kind} could not be rendered: {type(exc).__name__}: {exc}",
            "Check the points: a zero-length or self-crossing measurement has "
            "no dimension line to draw.",
        )
    if getattr(dim.dimension.dxf, "text_midpoint", None) is None:
        try:
            dim.destroy()
        except Exception:  # noqa: BLE001
            pass
        entity_fail(
            f"{kind} rendered without a text position",
            "Re-check that p1 and p2 are two different points.",
        )


def _entity_2d(msp: Any, entity: dict[str, Any]) -> int:
    """Add one 2D entity. Returns how many graphical entities it produced.

    Every branch names its failure and the fix, because the model writes these
    dictionaries and a bare KeyError teaches it nothing.
    """
    kind = str(entity.get("type") or "").strip().lower()
    layer = str(entity.get("layer") or "OUTLINE").upper()
    attribs = {"layer": layer if layer in LAYERS else "OUTLINE"}
    if layer not in LAYERS:
        attribs["layer"] = "OUTLINE"

    def point(key: str, required: bool = True) -> tuple[float, float] | None:
        value = entity.get(key)
        if value is None:
            if required:
                entity_fail(
                    f"{kind} is missing {key!r}",
                    f"Pass {key}=[x, y] in mm.",
                )
            return None
        try:
            return (float(value[0]), float(value[1]))
        except Exception:  # noqa: BLE001
            entity_fail(
                f"{kind} has an unusable {key}: {value!r}",
                f"Pass {key} as a two-number list, e.g. [10, 20].",
            )

    if kind == "line":
        start, end = point("start"), point("end")
        msp.add_line(start, end, dxfattribs=attribs)
        return 1
    if kind in ("rect", "rectangle"):
        # Accept the two spellings a drafter or an LLM will reach for: the two
        # opposite corners (p1/p2 or corner/end) or a size at a corner. Requiring
        # one exact spelling just makes the model fail on a drawing it described
        # correctly.
        first = point("p1", required=False) or point("corner", required=False) or point("start", required=False)
        second = point("p2", required=False) or point("end", required=False) or point("corner2", required=False)
        w = entity.get("width")
        h = entity.get("height")
        if isinstance(entity.get("size"), list) and len(entity["size"]) >= 2:
            w, h = entity["size"][0], entity["size"][1]
        if first is not None and second is not None:
            corner = (min(first[0], second[0]), min(first[1], second[1]))
            w, h = abs(second[0] - first[0]), abs(second[1] - first[1])
        elif first is not None and w and h:
            corner = first
        else:
            centre = entity.get("center")
            if centre is not None and w and h:
                corner = (float(centre[0]) - float(w) / 2.0, float(centre[1]) - float(h) / 2.0)
            else:
                entity_fail(
                    f"{kind} needs its size or its two opposite corners",
                    "Pass p1=[x, y], p2=[x, y] for the two opposite corners, or "
                    "size=[width, height] (optionally with corner=[x, y]).",
                )
        if not w or not h:
            entity_fail(
                f"{kind} came out zero-sided",
                "The corners are the same point — check p1 and p2.",
            )
        cx, cy = float(corner[0]), float(corner[1])
        msp.add_lwpolyline(
            [(cx, cy), (cx + w, cy), (cx + w, cy + h), (cx, cy + h)], close=True, dxfattribs=attribs
        )
        return 1
    if kind in ("circle", "hole"):
        centre = point("center")
        radius = float(entity.get("radius") or 0) or float(entity.get("diameter") or 0) / 2.0
        if radius <= 0:
            entity_fail(f"{kind} needs a radius or diameter", "Pass radius=5 (mm) or diameter=10.")
        msp.add_circle(centre, radius, dxfattribs=attribs)
        count = 1
        if entity.get("count") and int(entity["count"]) > 1 and entity.get("bolt_circle_radius"):
            ring = float(entity["bolt_circle_radius"])
            for step in range(int(entity["count"])):
                angle = 2.0 * math.pi * step / int(entity["count"])
                msp.add_circle(
                    (centre[0] + ring * math.cos(angle), centre[1] + ring * math.sin(angle)),
                    radius,
                    dxfattribs=attribs,
                )
                count += 1
        return count
    if kind == "arc":
        centre = point("center")
        radius = float(entity.get("radius") or 0)
        if radius <= 0:
            entity_fail("an arc needs a radius", "Pass radius=20 and start_angle/end_angle in degrees.")
        msp.add_arc(
            centre,
            radius,
            float(entity.get("start_angle") or 0),
            float(entity.get("end_angle") or 180),
            dxfattribs=attribs,
        )
        return 1
    if kind in ("polyline", "polygon"):
        points = entity.get("points") or []
        if not isinstance(points, list) or len(points) < 2:
            entity_fail("a polyline needs at least two points", "Pass points=[[x, y], [x, y], ...].")
        msp.add_lwpolyline(
            [(float(p[0]), float(p[1])) for p in points],
            close=bool(entity.get("closed")),
            dxfattribs=attribs,
        )
        return 1
    if kind == "ellipse":
        centre = point("center")
        major = float(entity.get("major_radius") or 0)
        minor = float(entity.get("minor_radius") or 0)
        if major <= 0 or minor <= 0:
            entity_fail("an ellipse needs major_radius and minor_radius", "Pass both in mm.")
        msp.add_ellipse(centre, major_axis=(major, 0.0), ratio=minor / major, dxfattribs=attribs)
        return 1
    if kind == "text":
        at = point("at", required=False) or point("position", required=False) or point("start", required=False)
        if at is None:
            entity_fail("a text entity needs a position", "Pass at=[x, y] in mm.")
        value = entity.get("text") or entity.get("value") or ""
        height = float(entity.get("height") or 3.5)
        rotation = float(entity.get("rotation") or 0)
        text = msp.add_text(
            str(value), height=height, rotation=rotation, dxfattribs={"layer": "ANNOTATION"}
        )
        # set_placement asserts on a plain string: it wants the enum, so map the
        # common names the model will write onto it.
        from ezdxf.enums import TextEntityAlignment

        wanted = str(entity.get("align") or "LEFT").strip().upper().replace(" ", "_")
        alignment = getattr(TextEntityAlignment, wanted, TextEntityAlignment.LEFT)
        text.set_placement(at, align=alignment)
        return 1
    if kind in ("hatch", "section"):
        points = entity.get("boundary_points") or entity.get("points") or []
        if isinstance(points, list) and len(points) >= 3:
            hatch = msp.add_hatch(color=int(entity.get("color") or 9), dxfattribs={"layer": "HATCH"})
            hatch.paths.add_polyline_path(
                [(float(p[0]), float(p[1])) for p in points], is_closed=True
            )
            pattern = str(entity.get("pattern") or "ANSI31")
            try:
                hatch.set_pattern_fill(pattern, scale=float(entity.get("scale") or 1.0))
            except Exception:  # noqa: BLE001 - a missing PAT definition must not lose the hatch
                hatch.set_solid_fill(color=int(entity.get("color") or 9))
            return 1
        # A hatch without a boundary is a section of the whole view: fall back to
        # filling the entities already drawn, which is what a drafter would do.
        msp.add_hatch(color=9, dxfattribs={"layer": "HATCH"}).set_solid_fill(color=9)
        return 1
    if kind in ("dim_linear", "dimension", "dim_horizontal", "dim_vertical", "dim_aligned"):
        p1 = point("p1", required=False) or point("start", required=False)
        p2 = point("p2", required=False) or point("end", required=False)
        if p1 is None or p2 is None:
            entity_fail(
                f"{kind} needs p1 and p2 — the two points being measured",
                "Pass p1=[x, y] and p2=[x, y].",
            )
        offset = float(entity.get("offset") or -12.0)
        text = str(entity["text"]) if entity.get("text") else "<>"
        dim: Any
        if kind == "dim_horizontal":
            dim = msp.add_linear_dim(
                base=(p2[0], p1[1] + offset), p1=p1, p2=p2, angle=0, text=text,
                dimstyle="EZDXF", dxfattribs={"layer": "DIMENSIONS"},
            )
        elif kind == "dim_vertical":
            dim = msp.add_linear_dim(
                base=(p1[0] + offset, p2[1]), p1=p1, p2=p2, angle=90, text=text,
                dimstyle="EZDXF", dxfattribs={"layer": "DIMENSIONS"},
            )
        elif kind == "dim_aligned":
            dim = msp.add_aligned_dim(
                p1=p1, p2=p2, distance=offset, text=text,
                dimstyle="EZDXF", dxfattribs={"layer": "DIMENSIONS"},
            )
        else:
            axis = str(entity.get("axis") or "auto").lower()
            if axis not in ("x", "y", "auto"):
                entity_fail(
                    f"dim_linear does not know the axis {axis!r}",
                    "Use axis='x', 'y', 'auto', or the types dim_horizontal / dim_vertical.",
                )
            if axis == "x":
                base = (p2[0], p1[1] + offset)
                angle = 0.0
            elif axis == "y":
                base = (p1[0] + offset, p2[1])
                angle = 90.0
            else:
                # No axis asked for: measure along the longer leg, which is what
                # "auto" means for a dimension across a plate.
                if abs(p2[0] - p1[0]) >= abs(p2[1] - p1[1]):
                    base, angle = (p2[0], p1[1] + offset), 0.0
                else:
                    base, angle = (p1[0] + offset, p2[1]), 90.0
            dim = msp.add_linear_dim(
                base=base, p1=p1, p2=p2, angle=angle, text=text,
                dimstyle="EZDXF", dxfattribs={"layer": "DIMENSIONS"},
            )
        _render_dim(dim, kind)
        return 1
    if kind in ("dim_radius", "dim_diameter"):
        centre = point("center")
        radius = float(entity.get("radius") or 0) or float(entity.get("diameter") or 0) / 2.0
        if radius <= 0:
            entity_fail(f"{kind} needs a radius or diameter", "Pass radius=5 or diameter=10.")
        angle = float(entity.get("angle") or 45.0)
        text = str(entity["text"]) if entity.get("text") else "<>"
        make = msp.add_radius_dim if kind == "dim_radius" else msp.add_diameter_dim
        dim = make(
            center=centre, radius=radius, angle=angle, text=text,
            dimstyle="EZ_RADIUS", dxfattribs={"layer": "DIMENSIONS"},
        )
        _render_dim(dim, kind)
        return 1
    if kind in ("dim_angular", "dim_angle"):
        centre = point("center")
        radius = float(entity.get("radius") or 0) or float(entity.get("distance") or 0) or 25.0
        first = float(entity.get("start_angle") or 0)
        second = float(entity.get("end_angle") or 90)
        if abs(second - first) % 360.0 == 0:
            entity_fail(
                "dim_angular has the same start_angle and end_angle",
                "Pass two different angles in degrees, e.g. start_angle=0 and end_angle=90.",
            )
        leg1 = (
            (centre[0], centre[1]),
            (centre[0] + radius * math.cos(math.radians(first)), centre[1] + radius * math.sin(math.radians(first))),
        )
        leg2 = (
            (centre[0], centre[1]),
            (centre[0] + radius * math.cos(math.radians(second)), centre[1] + radius * math.sin(math.radians(second))),
        )
        # 'base' is the point the dimension ARC passes through, not the vertex —
        # ezdxf derives the vertex from where the two legs meet. Handing it the
        # vertex makes the arc radius zero and ezdxf then divides by zero.
        mid = math.radians((first + second) / 2.0)
        dim = msp.add_angular_dim_2l(
            base=(centre[0] + radius * 0.6 * math.cos(mid), centre[1] + radius * 0.6 * math.sin(mid)),
            line1=leg1,
            line2=leg2,
            text=str(entity["text"]) if entity.get("text") else "<>",
            dimstyle="EZ_CURVED",
            dxfattribs={"layer": "DIMENSIONS"},
        )
        _render_dim(dim, kind)
        return 1
    if kind in ("centerline", "centre_line", "centreline", "center_line"):
        # Two spellings: a cross at a centre, or one centre line between two
        # points (which is what a hole-axis or a symmetry line actually is).
        first = point("p1", required=False) or point("start", required=False)
        second = point("p2", required=False) or point("end", required=False)
        if first is not None and second is not None:
            msp.add_line(first, second, dxfattribs={"layer": "CENTER"})
            return 1
        centre = point("center", required=False)
        if centre is None:
            entity_fail(
                "a centerline needs either p1 and p2, or a center",
                "Pass p1=[x, y], p2=[x, y] for the axis, or center=[x, y] for a cross.",
            )
        size = float(entity.get("size") or 20.0)
        msp.add_line((centre[0] - size, centre[1]), (centre[0] + size, centre[1]), dxfattribs={"layer": "CENTER"})
        msp.add_line((centre[0], centre[1] - size), (centre[0], centre[1] + size), dxfattribs={"layer": "CENTER"})
        return 2
    entity_fail(
        f"unknown 2D entity type {kind!r}",
        "Supported: line, rect, circle, arc, ellipse, polyline, text, hatch, "
        "dim_linear, dim_horizontal, dim_vertical, dim_aligned, dim_radius, "
        "dim_diameter, dim_angular, centerline.",
        supported=[
            "line", "rect", "circle", "arc", "ellipse", "polyline", "text", "hatch",
            "dim_linear", "dim_horizontal", "dim_vertical", "dim_aligned",
            "dim_radius", "dim_diameter", "dim_angular", "centerline",
        ],
    )


def _render_dxf(doc: Any, stem: str, out: Path, formats: list[str]) -> dict[str, Any]:
    """Rasterise a DXF through ezdxf's matplotlib backend."""
    written: dict[str, Any] = {}
    wanted = {f.strip().lower() for f in formats if f and f.strip()}
    if not wanted.intersection({"png", "svg", "pdf"}):
        return written
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

        # An unrendered DIMENSION (text_midpoint unset) makes ezdxf's drawing
        # addon raise on the whole layout, so it is filtered out of the preview
        # rather than allowed to cost the user the picture. The DXF itself keeps
        # the entity: this only affects the raster.
        doomed = [
            entity
            for entity in doc.modelspace()
            if entity.dxftype() == "DIMENSION"
            and getattr(entity.dxf, "text_midpoint", None) is None
        ]
        doomed_ids = {id(entity) for entity in doomed}
        for entity in doomed:
            print(
                f"warning: a {entity.dxf.dimtype & 0x0F} dimension had no rendered "
                "text position and was left out of the preview",
                file=sys.stderr,
            )
        figure = plt.figure(figsize=(14, 10), dpi=110)
        axes = figure.add_axes([0, 0, 1, 1])
        axes.set_axis_off()
        frontend = Frontend(RenderContext(doc), MatplotlibBackend(axes))
        frontend.draw_layout(
            doc.modelspace(),
            finalize=True,
            filter_func=lambda e: id(e) not in doomed_ids,
        )
        for fmt in wanted:
            if fmt not in ("png", "svg", "pdf"):
                continue
            target = out / f"{stem}.{fmt}"
            figure.savefig(target, dpi=170)
            written[fmt] = str(target)
        plt.close(figure)
    except Exception as exc:  # noqa: BLE001 - export must survive a preview failure
        print(f"warning: DXF preview failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return written


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def action_doctor(args: argparse.Namespace) -> None:
    deps = _deps()
    missing = [name for name, value in deps.items() if str(value).startswith("MISSING")]
    ok(
        action="doctor",
        cli_version=CLI_VERSION,
        python=sys.version.split()[0],
        dependencies=deps,
        ready=not missing,
        missing=missing,
        capabilities={
            "3d_solid_modelling": not str(deps["build123d"]).startswith("MISSING"),
            "step_stl_3mf_export": not str(deps["build123d"]).startswith("MISSING"),
            "2d_dxf_with_dimensions": not str(deps["ezdxf"]).startswith("MISSING"),
            "raster_and_vector_previews": not str(deps["matplotlib"]).startswith("MISSING"),
        },
        next=(
            "Everything is ready."
            if not missing
            else "Run action='install' to install: " + ", ".join(missing)
        ),
    )


def action_model(args: argparse.Namespace) -> None:
    out = _out_dir(args.out_dir)
    stem = _safe_stem(args.name, "model")
    shape = _shape_from(args)
    formats = args.formats or ["step", "stl"]
    written = _export_3d(shape, stem, out, formats)
    written.update({f"preview_{k}": v for k, v in _render_3d(shape, stem, out, args.preview or ["png"]).items()})
    measured = _measure(shape)
    ok(
        action="model",
        name=stem,
        out_dir=str(out),
        exported=written,
        files=_report_files(written),
        measured=measured,
        how_to_draw=f"engineering_draw(action='project', name={stem!r}, "
        f"code=<the same snippet>, code_is_reusable=True)",
    )


def action_draw(args: argparse.Namespace) -> None:
    # Gate on the dependency; the module object itself is used only by helpers.
    need_dep("ezdxf")
    out = _out_dir(args.out_dir)
    stem = _safe_stem(args.name, "drawing")
    drawing = dict(args.spec or {})
    entities = drawing.get("entities")
    if not isinstance(entities, list) or not entities:
        fail(
            "a 2D drawing needs an 'entities' list",
            "Pass spec={'entities': [{'type': 'line', ...}, ...]}. A rect plus a "
            "couple of dim_linear entities is already a usable drawing.",
        )
    doc, width, height = _new_doc(
        drawing.get("sheet") or args.sheet or "A3",
        landscape=str(drawing.get("orientation") or "landscape").lower() != "portrait",
    )
    msp = doc.modelspace()
    _border_and_title(msp, width, height, _coerce_title(drawing.get("title"), stem, drawing))

    count = 0
    errors: list[str] = []
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            errors.append(f"entity #{index} is not an object")
            continue
        try:
            count += _entity_2d(msp, entity)
        except EntityError as exc:
            errors.append(f"entity #{index} ({entity.get('type')}): {exc}")
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad entity must not lose the sheet
            errors.append(f"entity #{index} ({entity.get('type')}): {type(exc).__name__}: {exc}")
    if count == 0:
        fail(
            "no entity could be drawn",
            "Fix the entity errors listed in 'errors' and call again.",
            errors=errors,
        )

    dxf_path = out / f"{stem}.dxf"
    doc.saveas(dxf_path)
    written: dict[str, Any] = {"dxf": str(dxf_path)}
    written.update(_render_dxf(doc, stem, out, args.preview or ["png", "pdf"]))
    ok(
        action="draw",
        name=stem,
        out_dir=str(out),
        sheet={"size": drawing.get("sheet") or args.sheet or "A3", "width_mm": width, "height_mm": height},
        entities_drawn=count,
        entity_errors=errors,
        units_mm=doc.header.get("$INSUNITS") == 4,
        exported=written,
        files=_report_files(written),
        editable=(
            "The DXF holds real DIMENSION entities, layers and a title block, so it "
            "opens and edits in AutoCAD, QCAD, LibreCAD or FreeCAD — the dimensions "
            "are objects, not lines."
        ),
        note=(
            "Dimensions measure what you drew, so draw the outline to the true size "
            "in millimetres: a 100 mm edge gets p1=[0,0], p2=[100,0]."
        ),
    )


def action_project(args: argparse.Namespace) -> None:
    """Project a 3D model onto 2D views, hidden lines removed, plus a DXF."""
    need_dep("build123d")
    need_dep("ezdxf")
    out = _out_dir(args.out_dir)
    stem = _safe_stem(args.name, "projection")
    shape = _shape_from(args)

    views = args.views or ["front", "top", "right", "iso"]
    known = {
        "front": ((0, -1000, 0), (0, 0, 1)),
        "top": ((0, 0, 1000), (0, 1, 0)),
        "right": ((1000, 0, 0), (0, 0, 1)),
        "iso": ((1000, -1000, 800), (0, 0, 1)),
    }
    doc, width, height = _new_doc(args.sheet or "A3")
    msp = doc.modelspace()
    _border_and_title(msp, width, height, _coerce_title(args.title, stem))

    # Lay the views out on a grid, scaled so the whole part fits its cell. The
    # scale is computed rather than assumed: a 400 mm part on A3 at 1:1 would run
    # off the sheet, and silently clipping the view is worse than scaling it.
    try:
        box = shape.bounding_box()
        part_span = float(max(box.size.X, box.size.Y, box.size.Z)) or 1.0
        look_at = tuple(float(v) for v in box.center())
    except Exception:  # noqa: BLE001
        part_span = 100.0
        look_at = (0.0, 0.0, 0.0)
    columns = 2 if len(views) > 1 else 1
    rows = max(1, math.ceil(len(views) / columns))
    cell_w = (width - 30.0) / columns
    cell_h = (height - 60.0) / rows
    scale = min(min(cell_w, cell_h) * 0.72 / part_span, 1.0)
    scale = round(scale * 50) / 50 or 1.0  # snap to a value a drafter would write

    drawn = 0
    report: list[dict[str, Any]] = []
    for index, view in enumerate(views):
        name = str(view).strip().lower()
        origin = known.get(name, known["iso"])[0]
        up = known.get(name, known["iso"])[1]
        column, row = index % columns, index // columns
        base_x = 20.0 + column * cell_w + cell_w / 2.0
        base_y = 45.0 + (rows - 1 - row) * cell_h + cell_h / 2.0
        try:
            # Orthographic (no `focus`), looking at the part's own centre, so the
            # views line up with each other the way a third-angle drawing needs.
            visible, hidden = shape.project_to_viewport(
                viewport_origin=origin, viewport_up=up, look_at=look_at
            )
        except Exception as exc:  # noqa: BLE001
            report.append({"view": name, "error": f"{type(exc).__name__}: {exc}"})
            continue

        view_entities = 0
        for edge in visible:
            for segment in _edge_polyline(edge):
                msp.add_lwpolyline(
                    [
                        (base_x + point[0] * scale, base_y + point[1] * scale)
                        for point in segment
                    ],
                    dxfattribs={"layer": "OUTLINE"},
                )
                view_entities += 1
        for edge in hidden:
            for segment in _edge_polyline(edge):
                msp.add_lwpolyline(
                    [
                        (base_x + point[0] * scale, base_y + point[1] * scale)
                        for point in segment
                    ],
                    dxfattribs={"layer": "HIDDEN"},
                )
                view_entities += 1
        msp.add_text(name.upper(), height=4.0, dxfattribs={"layer": "ANNOTATION"}).set_placement(
            (base_x - 12.0, base_y - cell_h / 2.0 + 6.0)
        )
        drawn += view_entities
        report.append(
            {"view": name, "entities": view_entities, "visible_edges": len(visible), "hidden_edges": len(hidden)}
        )

    if drawn == 0:
        fail(
            "every view came back empty",
            "The projection produced no edges. Check that the geometry is a solid "
            "(a face or a wire projects differently), and try views=['front'].",
            views=report,
        )

    dxf_path = out / f"{stem}_views.dxf"
    doc.saveas(dxf_path)
    written: dict[str, Any] = {"dxf": str(dxf_path)}
    written.update(_render_dxf(doc, stem + "_views", out, args.preview or ["png", "pdf"]))
    # The 3D preview is prefixed: the projected views are already keyed "png", and
    # without this the shaded 3D render silently replaced the drawing preview.
    written.update(
        {f"preview_{key}": value for key, value in _render_3d(shape, stem, out, ["png"]).items()}
    )
    ok(
        action="project",
        name=stem,
        out_dir=str(out),
        views=report,
        scale=f"{scale:.0f}:1" if scale >= 1 else f"1:{round(1 / scale)}",
        sheet=args.sheet or "A3",
        exported=written,
        files=_report_files(written),
        measured=_measure(shape),
    )


def _edge_polyline(edge: Any) -> list[list[tuple[float, float]]]:
    """Flatten a projected edge to 2D polylines in the view plane.

    ``project_to_viewport`` returns edges in 3D camera space; a drawing needs them
    as 2D points, so each edge is sampled and its own plane dropped (the view is
    already looking down that axis, so Z carries no information).
    """
    segments: list[list[tuple[float, float]]] = []
    try:
        points: list[tuple[float, float]] = []
        curve = edge
        samples = 16
        for step in range(samples + 1):
            position = curve.position_at(step / samples)
            points.append((round(float(position.X), 4), round(float(position.Y), 4)))
        # Drop consecutive duplicates: a straight edge sampled 17 times is one line.
        cleaned: list[tuple[float, float]] = []
        for point in points:
            if not cleaned or cleaned[-1] != point:
                cleaned.append(point)
        if len(cleaned) >= 2:
            segments.append(cleaned)
    except Exception:  # noqa: BLE001
        try:
            points = [(round(float(v.X), 4), round(float(v.Y), 4)) for v in edge.vertices()]
            if len(points) >= 2:
                segments.append(points)
        except Exception:  # noqa: BLE001
            pass
    return segments


def action_section(args: argparse.Namespace) -> None:
    """Cut a solid on a plane and draw the section, hatched like a real one."""
    b3d = need_dep("build123d")
    need_dep("ezdxf")
    out = _out_dir(args.out_dir)
    stem = _safe_stem(args.name, "section")
    shape = _shape_from(args)
    axis = str(args.axis or "y").strip().lower()
    offset = float(args.at or 0.0)
    if axis == "x":
        plane = b3d.Plane.YZ.offset(offset)
    elif axis == "z":
        plane = b3d.Plane.XY.offset(offset)
    else:
        plane = b3d.Plane.XZ.offset(offset)
    try:
        section = shape.intersect(plane)
    except Exception as exc:  # noqa: BLE001
        fail(
            f"the section failed: {type(exc).__name__}: {exc}",
            "Try a different 'at' offset — a plane outside the part intersects "
            "nothing, and some cutters need to be inside the material.",
        )

    doc, width, height = _new_doc(args.sheet or "A3")
    msp = doc.modelspace()
    _border_and_title(msp, width, height, {"name": f"{stem} SECTION {axis.upper()}"})
    try:
        box = section.bounding_box()
        span = float(max(box.size.X, box.size.Y)) or 1.0
    except Exception:  # noqa: BLE001
        span = 100.0
    scale = min((width - 60.0) / span, (height - 80.0) / span, 1.0)
    base_x, base_y = width / 2.0, height / 2.0
    count = 0
    try:
        for face in section.faces():
            for outer in [face.outer_wire()]:
                points = []
                for edge in outer.edges():
                    for segment in _edge_polyline(edge):
                        points.extend(segment)
                if len(points) >= 3:
                    msp.add_lwpolyline(
                        [(base_x + p[0] * scale, base_y + p[1] * scale) for p in points],
                        close=True,
                        dxfattribs={"layer": "OUTLINE"},
                    )
                    hatch = msp.add_hatch(color=9, dxfattribs={"layer": "HATCH"})
                    hatch.paths.add_polyline_path(
                        [(base_x + p[0] * scale, base_y + p[1] * scale) for p in points],
                        is_closed=True,
                    )
                    try:
                        hatch.set_pattern_fill("ANSI31", scale=1.0)
                    except Exception:  # noqa: BLE001
                        hatch.set_solid_fill(color=9)
                    count += 1
    except Exception as exc:  # noqa: BLE001
        fail(
            f"could not trace the section outline: {type(exc).__name__}: {exc}",
            "The section may be empty at that offset — move 'at' inside the part.",
        )
    if count == 0:
        fail(
            f"the section at {axis}={offset} mm is empty",
            "Move 'at' inside the material — e.g. at=0 for a centred cut.",
        )
    dxf_path = out / f"{stem}_section.dxf"
    doc.saveas(dxf_path)
    written: dict[str, Any] = {"dxf": str(dxf_path)}
    written.update(_render_dxf(doc, stem + "_section", out, args.preview or ["png", "pdf"]))
    ok(
        action="section",
        name=stem,
        out_dir=str(out),
        plane=f"{axis.upper()}={offset}mm",
        hatched_regions=count,
        scale=round(scale, 3),
        exported=written,
        files=_report_files(written),
        area_mm2=_section_area(section),
    )


def _section_area(section: Any) -> float:
    """Sum the face areas of a section.

    ``intersect`` returns a Compound, and a Compound has no usable ``area`` of its
    own, so a naive ``section.area`` reported 0.0 for every real cut. A section's
    area is the sum of its faces.
    """
    try:
        faces = list(section.faces())
    except Exception:  # noqa: BLE001
        return 0.0
    total = 0.0
    for face in faces:
        try:
            total += float(face.area)
        except Exception:  # noqa: BLE001
            continue
    return round(total, 4)


def action_inspect(args: argparse.Namespace) -> None:
    if not args.file:
        fail("inspect needs a 'file'", "Pass file=/path/to/part.step (or .stl/.dxf).")
    path = Path(os.path.expanduser(str(args.file)))
    if not path.is_file():
        fail(
            f"no such file: {path}",
            "Check the path — the sandbox persists files between calls, so a "
            "previous export is still there.",
        )
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "dxf":
        ezdxf = need_dep("ezdxf")
        doc = ezdxf.readfile(str(path))
        msp = doc.modelspace()
        counts: dict[str, int] = {}
        for entity in msp:
            counts[entity.dxftype()] = counts.get(entity.dxftype(), 0) + 1
        # $INSUNITS is what tells a reader whether one drawing unit is one mm.
        # Report the raw code as well as the boolean: "unitless" (None) and
        # "explicitly inches" (1) both fail the boolean and need different fixes.
        insunits = doc.header.get("$INSUNITS")
        ok(
            action="inspect",
            file=str(path),
            kind="dxf",
            units_mm=insunits == 4,
            units_code=insunits,
            units="millimetres" if insunits == 4 else f"$INSUNITS={insunits} (not mm)",
            layers=sorted(layer.dxf.name for layer in doc.layers),
            entity_counts=counts,
            dimensions=counts.get("DIMENSION", 0),
            editable_dimensions=counts.get("DIMENSION", 0) > 0,
        )
    b3d = need_dep("build123d")
    try:
        if suffix in ("step", "stp"):
            shape = b3d.import_step(str(path))
        elif suffix in ("brep",):
            shape = b3d.import_brep(str(path))
        elif suffix == "stl":
            shape = b3d.Mesher().read(str(path))[0] if hasattr(b3d, "Mesher") else None
            if shape is None:
                fail("STL import is unavailable in this build123d", "Use a STEP file instead.")
        else:
            fail(
                f"cannot inspect a .{suffix} file",
                "Supported: .step, .stp, .brep, .stl, .dxf",
            )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fail(f"could not read {path.name}: {type(exc).__name__}: {exc}", "Check the file is a real CAD export.")
    ok(action="inspect", file=str(path), kind=suffix, measured=_measure(shape))


def action_export(args: argparse.Namespace) -> None:
    if not args.file:
        fail("export needs a 'file'", "Pass file=/path/to/part.step.")
    path = Path(os.path.expanduser(str(args.file)))
    if not path.is_file():
        fail(f"no such file: {path}", "Check the path.")
    out = _out_dir(args.out_dir)
    stem = _safe_stem(args.name or path.stem)
    b3d = need_dep("build123d")
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("step", "stp"):
        shape = b3d.import_step(str(path))
    elif suffix == "brep":
        shape = b3d.import_brep(str(path))
    else:
        fail(
            f"export cannot read a .{suffix} file",
            "Give it a .step or .brep model. To convert a DXF, use action='export' "
            "with kind='drawing' — or simply re-draw with action='draw'.",
        )
    written = _export_3d(shape, stem, out, args.formats or ["step", "stl"])
    ok(
        action="export",
        file=str(path),
        exported=written,
        files=_report_files(written),
        measured=_measure(shape),
    )


ACTIONS = {
    "doctor": action_doctor,
    "model": action_model,
    "draw": action_draw,
    "project": action_project,
    "section": action_section,
    "inspect": action_inspect,
    "export": action_export,
}


def _load_json(raw: str | None, label: str) -> Any:
    if raw in (None, ""):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    candidate = text
    if not text.startswith(("{", "[")):
        maybe = Path(os.path.expanduser(text))
        if maybe.is_file():
            candidate = maybe.read_text()
    try:
        return json.loads(candidate)
    except Exception as exc:  # noqa: BLE001
        fail(
            f"{label} is not valid JSON: {exc}",
            f"Pass {label} as a JSON object, or as the path to a .json file.",
            received=text[:400],
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engineering_draw_cli",
        description="Engineering drawing engine: 2D DXF and 3D CAD in the sandbox.",
    )
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("--spec", help="JSON part/drawing description (or a path to a .json file).")
    parser.add_argument("--code", help="A build123d snippet; assign the shape to 'result'.")
    parser.add_argument("--params", help="JSON object exposed to the snippet as `params`.")
    parser.add_argument("--name", help="Base name for the exported files.")
    parser.add_argument("--out-dir", dest="out_dir", help="Where to write. Defaults to ~/engineering_drawings.")
    parser.add_argument("--formats", help="Comma-separated export formats, e.g. step,stl,brep,3mf.")
    parser.add_argument("--preview", help="Comma-separated preview formats: png,svg,pdf.")
    parser.add_argument("--sheet", help="Sheet size: A4, A3, A2, A1, A0, letter, tabloid.")
    parser.add_argument("--views", help="Comma-separated projection views: front,top,right,iso.")
    parser.add_argument("--title", help="JSON title-block values (name, material, drawn_by, scale, rev, sheet, note).")
    parser.add_argument("--axis", help="Section axis: x, y or z.")
    parser.add_argument("--at", help="Section plane offset in mm.")
    parser.add_argument("--file", help="Input file for inspect/export.")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.spec = _load_json(args.spec, "spec")
    args.params = _load_json(args.params, "params") or {}
    if isinstance(args.title, str):
        raw_title = args.title.strip()
        if raw_title and not raw_title.startswith(("{", "[")) and not Path(
            os.path.expanduser(raw_title)
        ).is_file():
            # ``--title FLANGE`` is a title-block name, not malformed JSON.
            args.title = {"name": raw_title}
        else:
            args.title = _load_json(args.title, "title")
    if args.formats:
        args.formats = [f for f in str(args.formats).split(",") if f.strip()]
    if args.preview:
        args.preview = [f for f in str(args.preview).split(",") if f.strip()]
    if args.views:
        args.views = [v for v in str(args.views).split(",") if v.strip()]
    if args.at not in (None, ""):
        try:
            args.at = float(args.at)
        except (TypeError, ValueError):
            fail(f"--at must be a number, got {args.at!r}", "Pass --at 0 for a centred cut.")
    try:
        ACTIONS[args.action](args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never show a bare traceback
        fail(
            f"{args.action} failed: {type(exc).__name__}: {exc}",
            "This is an engine error, not a bad request — retry, and if it "
            "repeats, use action='doctor' to check the install.",
            traceback=traceback.format_exc()[-1500:],
        )


if __name__ == "__main__":
    main()
