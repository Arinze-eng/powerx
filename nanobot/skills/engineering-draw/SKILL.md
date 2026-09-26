---
name: engineering-draw
description: "Design real 2D engineering drawings and 3D CAD parts, and export them, inside the user's execution sandbox (build123d + ezdxf, never on the app host). Use when the user asks for an engineering/technical drawing, a DXF, a part model, a STEP/STL/3MF export, an orthographic or section view, a mechanical design, a bracket/plate/flange/shaft, or a CAD file."
metadata: {"nanobot":{"emoji":"📐","requires":{"tools":["engineering_draw"],"env":["NOVITA_API_KEY"]}}}
---

# Engineering drawings and 3D CAD (sandbox-only)

The `engineering_draw` tool runs `build123d` (3D solids) and `ezdxf` (2D drawings)
**inside the user's execution sandbox**. OpenCASCADE is hundreds of megabytes and
has no business on the application host, so the engine is a CLI that lives in the
sandbox and this tool only drives it — the same arrangement as `mt5_sandbox`.

Everything is **millimetres**. Angles are degrees.

## The two things it makes

**A drawing** (`action="draw"`) — a bordered sheet with a title block and
*layers*, and crucially **real DXF `DIMENSION` entities**: `dim_linear`,
`dim_vertical`, `dim_aligned`, `dim_radius`, `dim_diameter`, `dim_angular`. These
are parametric objects, not exploded lines, so the user opens the file in AutoCAD,
QCAD, LibreCAD or FreeCAD and the dimensions are still editable, still associated
with the geometry, and still re-scale with the drawing. That editability is
usually the whole point — never substitute a picture of a drawing for it.

**A part** (`action="model"`) — a real solid, exported as **STEP** (what a
machinist and every CAM tool read), plus STL, 3MF, BREP, OBJ, glTF, and a shaded
PNG preview so a human can see it without a CAD viewer.

## Deciding: `spec` or `code`?

`spec` is a JSON description. It covers the shapes most parts actually are — a
box, plate, cylinder, shaft, sphere, cone or tube, with drilled holes, bolt
circles, fillets and chamfers. Prefer it: it does not depend on getting Python
exactly right, and it fails with a named field rather than a traceback.

```json
{"kind": "plate", "length": 120, "width": 80, "height": 12,
 "holes": [{"diameter": 10, "at": [0, 0]},
           {"diameter": 6, "count": 4, "bolt_circle_radius": 28}]}
```

`code` is a build123d snippet, and it is the escape hatch that makes the engine
general. Assign the shape to `result`; `params` is in scope:

```python
result = Box(50, 30, 10) - Cylinder(6, 40)
```

Reach for `code` the moment the part is not a primitive with holes — a bracket
with an angled web, a profile you extrude, a part you revolve. Do not contort a
`spec` to fit.

A model and its drawing are the **same** input: pass the same `spec` or the same
`code` to `action="project"` and you get the drawing of the part you just built.

## Drawing a real drawing

`action="draw"` takes `spec` as a sheet description. Draw the outline at its
**true size in millimetres** — the dimensions measure the geometry you draw, so a
100 mm edge is `p1=[0,0], p2=[100,0]`, not a scaled sketch.

```json
{"sheet": "A4", "orientation": "landscape",
 "title": {"name": "FLANGE", "material": "AL 6082", "drawn_by": "powerx", "scale": "1:1"},
 "entities": [
   {"type": "rect", "p1": [0, 0], "p2": [100, 60]},
   {"type": "circle", "center": [50, 30], "radius": 18},
   {"type": "centerline", "p1": [50, -6], "p2": [50, 66]},
   {"type": "dim_linear", "p1": [0, 0], "p2": [100, 0], "offset": -14, "axis": "x"},
   {"type": "dim_linear", "p1": [0, 0], "p2": [0, 60], "offset": -14, "axis": "y"},
   {"type": "dim_radius", "center": [50, 30], "radius": 18, "angle": 45, "text": "R18"},
   {"type": "dim_angular", "center": [50, 30], "radius": 19, "start_angle": 20, "end_angle": 70},
   {"type": "text", "at": [8, 72], "text": "PLATE 100x60x12", "height": 5}
 ]}
```

Entity types: `line`, `rect`, `circle` (with optional `count` +
`bolt_circle_radius`), `arc`, `ellipse`, `polyline`, `text`, `hatch`,
`centerline`, and the six dimension types. A rect takes either its two opposite
corners (`p1`/`p2`) or `size`/`width`+`height`. A `centerline` takes two points
for an axis, or a `center` for a cross. Give dimensions an `offset` in mm to push
the dimension line clear of the part; negative puts it below/left.

**Layers are already set up** and entities land on sensible ones: OUTLINE, HIDDEN,
CENTER, DIMENSIONS, ANNOTATION, HATCH, BORDER, TITLE. Pass `"layer": "HIDDEN"` on
an entity to override.

Do not put all the measurements on the drawing. Dimension what a machinist needs
to make the part: overall size, hole positions and diameters, and any feature
whose size is not obvious.

## From 3D to 2D

`action="project"` projects a solid into `front`, `top`, `right` and `iso` views,
hidden lines removed and drawn on the HIDDEN layer, laid out on a sheet and
scaled to fit (`--views front,top,right,iso`). `action="section"` cuts a solid on
a plane (`axis` x/y/z, `at` an mm offset) and hatches the cut — `at=0` cuts through
the centre. Both need the **same `spec` or `code`** as the model.

## Files, output and where they go

Output defaults to `~/engineering_drawings` in the sandbox and **persists between
calls**, so a model built in one call can be projected, sectioned, inspected or
exported in the next. Every action returns `files` with each artefact's path,
existence and byte size — read that to know what actually got written, and never
claim a file exists because the action succeeded.

`inspect` measures an existing `.step`/`.stp`/`.brep`/`.stl`/`.dxf` — bounding
box, volume, solid count, face/edge count, and mass at steel density. For a DXF it
also reports the entity census and whether the dimensions are editable objects.
Use it to confirm a drawing really carries `DIMENSION` entities rather than
asserting it.

## Install once, then draw

First call in a session may report `build123d` missing. The order is:

1. `action="doctor"` — what is installed and what is ready.
2. `action="install"` if something is missing. It runs **detached** (OpenCASCADE
   is a slow download) and returns immediately.
3. `action="status"` — poll this **yourself** until it reports `ready`, then
   `doctor` again. Never tell the user to check back, and never report a drawing
   as done while the engine is still installing.
4. Then draw.

The sandbox runs as an unprivileged user, so the installer reaches the system
libraries OpenCASCADE wants through passwordless `sudo`, treats an apt failure as
a warning, and calls the install a success **only** once `build123d`, `ezdxf` and
`matplotlib` all import. `pip` exiting 0 is not the acceptance test.

## Reporting back

Say what was made and in what format, name the file paths, and quote the measured
numbers (volume, mass, bounding box) rather than paraphrasing them. If a drawing
or an export came back with `entity_errors`, or an export failed, say so in plain
terms and say which artefact is missing — a partial result reported honestly is
worth more than a confident summary that is wrong about what exists.
