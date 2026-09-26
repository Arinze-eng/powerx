---
name: engineering-draw
description: "Design real 2D engineering drawings and 3D CAD parts, watch the design happen on the live screen, and export DXF/STEP/STL/PNG — inside the user's execution sandbox (FreeCAD first, build123d as fallback, never on the app host). Use when the user asks for an engineering/technical drawing, a DXF, a part model, a STEP/STL/3MF export, a render of a 3D design, an orthographic or section view, a mechanical design, a bracket/plate/flange/shaft, or a CAD file."
metadata: {"nanobot":{"emoji":"📐","requires":{"tools":["engineering_draw"],"env":["NOVITA_API_KEY"]}}}
---

# Engineering drawings and 3D CAD (sandbox-only)

The `engineering_draw` tool runs a CAD engine **inside the user's execution
sandbox**. OpenCASCADE and FreeCAD are hundreds of megabytes and have no business
on the application host, so the engine is a CLI that lives in the sandbox and this
tool only drives it — the same arrangement as `mt5_sandbox`.

Everything is **millimetres**. Angles are degrees.

## FreeCAD is the engine you use first

For any design task — 2D or 3D, model or export — reach for **`action="freecad"`**
and then **`action="freecad_gui"`**. FreeCAD carries a real document, the Draft
workbench, TechDraw sheets, the STEP/DXF writers, and a GUI window the live screen
can stream; `build123d` has a kernel and nothing else. So:

| task | action |
| --- | --- |
| design a part or a drawing, and export it | `freecad` |
| let the user **watch** it being designed | `freecad_gui` |
| a solid from a JSON spec, no FreeCAD | `model` (fallback) |
| a 2D drawing with editable DXF dimensions, no FreeCAD | `draw` (fallback) |
| orthographic views / a section of a solid | `project`, `section` |

Use the build123d fallbacks **only** when `action="doctor"` says FreeCAD is
unavailable. Say which engine you used in the reply.

### Design and export with `freecad`

`code` is a FreeCAD python snippet. It runs inside a document already bound to
`doc`, with `FreeCAD`, `Part`, `Draft`, `Import`, `Mesh` and `TechDraw` in scope,
plus one helper:

```python
add_page(objects=None, template="A4_LandscapeTD.svg", direction=None, views=None, scale=None)
```

- `objects` — what to sheet. Omit it and the **result solids of the design** are
  used: the shapes you left live, with every construction solid they consumed
  filtered out. That is almost always what you want, so `add_page()` with no
  arguments sheets the finished part.
- `direction` — one view, `front`, `top`, `right`, `left`, `rear`, `bottom` or `iso`.
- `views` — several projections on **one sheet**, laid out in a grid:
  `add_page(views=["front", "right", "top", "iso"])`. Prefer this: a drawing a
  machinist can actually read is three orthographic views plus an iso, not one
  lone view. Up to four fit an A4 landscape sheet.

```python
plate = doc.addObject("Part::Box", "Plate")
plate.Length, plate.Width, plate.Height = 80.0, 50.0, 12.0
bore = doc.addObject("Part::Cylinder", "Bore")
bore.Radius, bore.Height = 8.0, 20.0
bore.Placement.Base = FreeCAD.Vector(40, 25, 0)
body = doc.addObject("Part::Cut", "Bracket")
body.Base, body.Tool = plate, bore
doc.recompute()
page = add_page([body], direction="iso")
```

A part built as a chain of cuts wants the multi-view sheet, and wants it to
sheet the finished body rather than the scaffolding:

```python
plate = doc.addObject("Part::Box", "Plate")
plate.Length, plate.Width, plate.Height = 80.0, 50.0, 12.0
bore = doc.addObject("Part::Cylinder", "Bore")
bore.Radius, bore.Height = 8.0, 60.0
bore.Placement.Base = FreeCAD.Vector(40, 25, -10)
current = doc.addObject("Part::Cut", "PlateBored")
current.Base, current.Tool = plate, bore
for i, (x, y) in enumerate([(8.0, 8.0), (72.0, 8.0)]):
    hole = doc.addObject("Part::Cylinder", "Hole%d" % i)
    hole.Radius, hole.Height = 3.2, 40.0
    hole.Placement.Base = FreeCAD.Vector(x, y, -10)
    cut = doc.addObject("Part::Cut", "Stage%d" % i)
    cut.Base, cut.Tool = current, hole
    current = cut
doc.recompute()
# The finished body only -- Plate, Bore and every Stage are scaffolding.
page = add_page(views=["front", "right", "top", "iso"])
```

Read `objects` in the result back to yourself: it lists the solids that were
measured, so it is the check on whether you sheeted the part or its scaffolding.
`measured.volume_mm3` and `measured.bbox_mm` are that part's, not the sum of
everything you built. If a snippet leaves several live solids side by side, each
is a result and their volumes are summed — a note says so, and overlapping solids
then double-count, so fuse them into one body with `Part::MultiFuse` instead.

`formats` accepts `step`, `stp`, `iges`, `brep`, `stl`, `dxf`, `svg`, `png`,
`pdf`. The `.FCStd` document is always saved too.

**`add_page(...)` is what makes a drawing.** Without it `svg`, `png` and `pdf`
have nothing to render and come back with a note saying so — they are sheet
formats, not model formats. `dxf` works either way and means different things:

- **with** a page it is the **sheet** — frame, title block and the projected view.
- **without** a page it is the raw 2D entities in the document.

Both are real DXF a reader opens. Read `exported` and `files` in the result, not
your hopes: every action reports each artefact's path and byte size, and an export
that failed arrives in `freecad_errors` while the others still succeeded. A partial
result reported honestly beats a confident summary that is wrong about what exists.

### Show the user with `freecad_gui`

`freecad_gui` builds or opens the design and **opens it in FreeCAD's GUI on the
sandbox display**. The live screen panel captures that display, so opening the
window *is* the live view — the user watches the part being drawn in the CAD app
itself. Nothing else is needed to stream it.

- `file` — open an existing model or `.FCStd` instead of building one. Chain it
  after `freecad` with `file=~/engineering_drawings/bracket.FCStd`.
- `preview_view` — `iso`, `front`, `top`, `right`, `left` or `rear`. This also
  writes the **PNG** (`bracket_view.png`), which is the picture to quote when
  someone asks to see the 3D design. Without it you get the window and no still.
  Be exact about what that PNG is: a photograph of FreeCAD's window on the
  display, so it shows whatever direction that window happens to be facing.
  `preview_view` steers the *drawing* views — every sheet `add_page([...])` builds
  projects from it — which is what the dxf/svg/pdf carry.

Use `freecad_gui` whenever the user is watching or wants an image of a 3D design;
use `freecad` alone when you only need files.

Start the display before you need it — `freecad_gui` does this itself, but the
installer does it too, and both are idempotent.

## Install once, then draw

**FreeCAD must be installed before it can be used, and you never install it by
hand.** The order is:

1. `action="doctor"` — what is installed; it names `preferred_engine`, and reports
   `freecad.available`, its `version`, the `display`, and whether the GUI is there.
2. `action="install"` if FreeCAD or the python packages are missing. It runs
   **detached** — it apt-installs `freecad`, `xvfb`, a window manager and
   `rsvg-convert`, then pip-installs build123d/ezdxf/matplotlib — and returns
   immediately.
3. `action="status"` — poll this **yourself** until it reports done, then `doctor`
   again. Never tell the user to check back, and never report a drawing as done
   while the engine is still installing.
4. Then design.

The install writes `install.json` and `freecad.status` in the sandbox so both the
model and a human can see what landed. It is called a success once FreeCAD builds
and exports a solid, or — if FreeCAD is genuinely unavailable on the image —
once `build123d`, `ezdxf` and `matplotlib` import, in which case the fallback
actions are what you use. `apt` failing is a warning, never a failed install, and
`pip` exiting 0 is not the acceptance test.

One measured trap, so you recognise it rather than chasing it: on this image
FreeCAD's binary links a from-source python at `/usr/local` and its embedded
interpreter then dies with `No module named 'math'`, which reads like a broken
install and is not one. The engine exports `PYTHONHOME=/usr` and
`LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu` for every FreeCAD call and that fixes
it. Do not "repair" FreeCAD by reinstalling it.

## The fallbacks, when FreeCAD is not there

**A part** (`action="model"`) — a solid from a JSON `spec`
(`{"kind":"plate","length":120,"width":80,"height":12,"holes":[{"diameter":10,"at":[0,0]}]}`)
or a build123d `code` snippet assigning the shape to `result`, exported as STEP,
STL, 3MF, BREP, OBJ or glTF with a shaded preview.

**A drawing** (`action="draw"`) — a bordered sheet with a title block and real DXF
`DIMENSION` entities (`dim_linear`, `dim_vertical`, `dim_aligned`, `dim_radius`,
`dim_diameter`, `dim_angular`). They are parametric, so the user opens the file and
the dimensions are still editable and still re-scale with the drawing — that
editability is usually the whole point. Never substitute a picture of a drawing for
it. Draw the outline at **true size**: a 100 mm edge is `p1=[0,0], p2=[100,0]`.
Entity types: `line`, `rect`, `circle` (with optional `count` +
`bolt_circle_radius`), `arc`, `ellipse`, `polyline`, `text`, `hatch`, `centerline`,
and the six dimension types. Layers are pre-set (OUTLINE, HIDDEN, CENTER,
DIMENSIONS, ANNOTATION, HATCH, BORDER, TITLE); override with `"layer": "HIDDEN"`.

Dimension what a machinist needs to make the part — overall size, hole positions
and diameters, anything not obvious. Do not dimension every edge.

**`project`** projects a solid into `front`/`top`/`right`/`iso` with hidden lines
on the HIDDEN layer; **`section`** cuts it on `axis` x/y/z at `at` mm and hatches
the cut. Both take the same `spec` or `code` as the model.

## Files and output

Output defaults to `~/engineering_drawings` in the sandbox and **persists between
calls**, so a model built in one call can be opened in the GUI, projected, sectioned
or exported in the next. `inspect` measures an existing
`.step`/`.stp`/`.brep`/`.stl`/`.dxf` — bounding box, volume, solid count,
face/edge count, mass at steel density — and for a DXF reports the entity census
and whether the dimensions are editable objects. Use it to confirm a claim rather
than asserting it.

## Reporting back

Say what was made, in what formats, and with which engine; name the file paths, and
quote measured numbers (volume, mass, bounding box) rather than paraphrasing them.
If an export failed, or a format came back with a note explaining why it needs a
page, say which artefact is missing in plain terms.
