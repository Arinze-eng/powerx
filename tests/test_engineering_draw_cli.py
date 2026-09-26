"""Tests for ``scripts/engineering_draw_cli.py``, the sandbox-side engine.

These cover the parts that do not need build123d or ezdxf: the JSON protocol, the
argument contract the host tool builds against, the entity dispatch, and the
drawing rules. The CAD half is proved separately against a live sandbox, where
OpenCASCADE actually exists -- a stubbed OCP would only test the stub.

Each case here is a defect that was reproduced live in a Novita sandbox on
2026-09-26, or a contract the host tool depends on:

1. **``rect`` rejected ``p1``/``p2``.** A drawing described perfectly correctly
   failed with "a rect needs a size [w, h] or an opposite corner", because only
   one of the two natural spellings was accepted.
2. **``fail()`` inside an entity aborted the whole sheet.** Twenty entities, one
   bad dictionary, nineteen lost.
3. **An unrendered DIMENSION crashed the raster preview.** ezdxf's drawing addon
   reads ``text_midpoint.z``, which is None until ``render()`` runs, so one bad
   dimension cost the user the entire picture.
4. **``add_angular_dim_2l``'s ``base`` is the arc location, not the vertex.**
   Passing the vertex makes the arc radius zero and ezdxf divides by zero.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "engineering_draw_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("engineering_draw_cli_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()

#: ezdxf is a light pure-python dependency, so the 2D half runs everywhere.
ezdxf = pytest.importorskip("ezdxf", reason="the 2D half of the engine needs ezdxf")


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
def test_ok_prints_one_json_object_and_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.ok(action="doctor", ready=True)
    assert exit_info.value.code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "action": "doctor", "ready": True}


def test_fail_prints_one_json_object_and_exits_nonzero(capsys) -> None:
    """The host tool reads ``ok`` to decide error vs output, so it must be there."""
    with pytest.raises(SystemExit) as exit_info:
        cli.fail("nope", "do this instead", extra=1)
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"] == "nope"
    assert payload["next"] == "do this instead"
    assert payload["extra"] == 1


def test_a_missing_dependency_names_the_install_action(capsys) -> None:
    """A missing package must not look like a bad request."""
    with pytest.raises(SystemExit):
        cli.need_dep("nonexistent_package_xyz")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "install" in payload["next"]
    assert payload["missing"] == ["nonexistent_package_xyz"]


def test_no_missing_dependency_returns_the_module() -> None:
    assert cli.need_dep("json") is json


# --------------------------------------------------------------------------- #
# Small helpers the tool relies on
# --------------------------------------------------------------------------- #
def test_safe_stem_strips_path_separators() -> None:
    """"name" reaches the CLI from the model, so it must not escape the out dir."""
    assert "/" not in cli._safe_stem("../../etc/passwd")
    assert cli._safe_stem("") == "drawing"
    assert cli._safe_stem("!!!") == "drawing"
    assert cli._safe_stem("bracket v2") == "bracket_v2"


def test_out_dir_expands_the_home_tilde(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = cli._out_dir("~/drawings")
    assert resolved == (tmp_path / "drawings").resolve()
    assert resolved.is_dir()


def test_sheet_geometry_knows_the_standard_sizes() -> None:
    assert cli._sheet_geometry("A4") == (210.0, 297.0)
    assert cli._sheet_geometry("A3") == (297.0, 420.0)


def test_an_unknown_sheet_is_named_with_its_alternatives(capsys) -> None:
    with pytest.raises(SystemExit):
        cli._sheet_geometry("A9")
    payload = json.loads(capsys.readouterr().out.strip())
    assert "A9" in payload["error"]
    assert "A4" in payload["supported"]


def test_report_files_marks_what_actually_exists(tmp_path) -> None:
    real = tmp_path / "part.step"
    real.write_text("solid")
    report = cli._report_files({"step": str(real), "stl": str(tmp_path / "missing.stl")})
    assert report["step"] == {"path": str(real), "exists": True, "bytes": 5}
    assert report["stl"]["exists"] is False


# --------------------------------------------------------------------------- #
# The drawing engine
# --------------------------------------------------------------------------- #
def _msp():
    doc, width, height = cli._new_doc("A4")
    return doc, doc.modelspace(), width, height


def test_new_doc_sets_millimetres_and_the_drafting_layers() -> None:
    doc, _msp_, width, height = _msp()
    assert doc.header["$INSUNITS"] == 4, "a reader must not have to guess the units"
    assert (width, height) == (297.0, 210.0), "A4 landscape"
    for layer in ("OUTLINE", "HIDDEN", "CENTER", "DIMENSIONS", "ANNOTATION", "BORDER", "TITLE"):
        assert layer in doc.layers


def test_a_dimensioned_sheet_rasterises() -> None:
    """The regression that mattered most in the 2D half.

    MEASURED FAILURE (2026-09-26): the first live drawing produced a DXF but no
    PNG -- ``warning: DXF preview failed: AttributeError: 'NoneType' object has no
    attribute 'z'``. ezdxf's drawing addon reads ``DIMENSION.dxf.text_midpoint.z``,
    which is None until ``render()`` runs, so a single dimension that had not been
    rendered cost the user the whole picture. An earlier revision also set
    ``dimblk = "ArchTick"`` on every dimstyle; the built-in arrow names ezdxf ships
    (``ARCHTICK``, ``CLOSEDBLANK``) are renderer special cases, so overriding them
    was both unnecessary and a second way to break the same raster.
    """
    import tempfile

    doc, msp, width, height = _msp()
    cli._border_and_title(msp, width, height, {"name": "RASTER"})
    entities = [
        {"type": "rect", "p1": [0, 0], "p2": [100, 60]},
        {"type": "circle", "center": [50, 30], "radius": 10},
        {"type": "dim_linear", "p1": [0, 0], "p2": [100, 0], "axis": "x"},
        {"type": "dim_linear", "p1": [0, 0], "p2": [0, 60], "axis": "y"},
        {"type": "dim_aligned", "p1": [0, 0], "p2": [30, 40]},
        {"type": "dim_radius", "center": [50, 30], "radius": 10},
        {"type": "dim_diameter", "center": [50, 30], "radius": 10, "angle": 225},
        {
            "type": "dim_angular",
            "center": [50, 30],
            "radius": 10,
            "start_angle": 0,
            "end_angle": 90,
        },
    ]
    for entity in entities:
        assert cli._entity_2d(msp, entity) == 1
    dims = msp.query("DIMENSION")
    assert len(dims) == 6
    assert all(entity.dxf.text_midpoint is not None for entity in dims), (
        "an unrendered dimension makes the raster raise on the whole layout"
    )

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        written = cli._render_dxf(doc, "raster", out, ["png"])
        assert written.get("png"), "the preview must be produced"
        assert Path(written["png"]).stat().st_size > 5000


def test_border_and_title_block_are_drawn_on_their_own_layers() -> None:
    doc, msp, width, height = _msp()
    cli._border_and_title(msp, width, height, {"name": "FLANGE", "material": "AL 6082"})
    layers = {entity.dxf.layer for entity in msp}
    assert {"BORDER", "TITLE"} <= layers
    texts = [e for e in msp if e.dxftype() == "TEXT"]
    assert any("FLANGE" in e.dxf.text for e in texts)
    assert any("AL 6082" in e.dxf.text for e in texts)


def test_a_title_can_be_a_plain_string() -> None:
    """MEASURED FAILURE (2026-09-26): ``"title": "FLANGE"`` crashed the sheet.

    The title block called ``title.get(...)``, so a title written as a bare
    string -- the shorthand, and the same thing to a drafter -- died with
    ``AttributeError: 'str' object has no attribute 'get'``. Worse, ``main``
    classified the crash as "an engine error, not a bad request", which is the one
    message that tells the model to retry the identical call forever.
    """
    doc, msp, width, height = _msp()
    cli._border_and_title(msp, width, height, cli._coerce_title("FLANGE", "drawing"))
    assert any(e.dxf.text == "FLANGE" for e in msp if e.dxftype() == "TEXT")


def test_a_title_that_is_neither_a_dict_nor_a_string_is_named(capsys) -> None:
    with pytest.raises(SystemExit):
        cli._coerce_title(42, "drawing")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "title" in payload["error"]
    assert "next" in payload, "an unreadable field must name the shape that was wanted"


def test_drawing_level_material_and_scale_reach_the_title_block() -> None:
    """Writing ``"scale": "1:2"`` next to ``"sheet": "A3"`` is what everyone does
    first; silently dropping it leaves the drawing claiming 1:1."""
    title = cli._coerce_title(
        None, "flange", {"material": "AL 6082", "scale": "1:2", "sheet": "A3"}
    )
    assert title == {"name": "flange", "material": "AL 6082", "scale": "1:2"}
    explicit = cli._coerce_title({"scale": "2:1"}, "flange", {"scale": "1:2"})
    assert explicit["scale"] == "2:1", "the title block itself must win"
    assert cli._coerce_title(None, "drawing") == {"name": "drawing"}
    assert cli._coerce_title("", "drawing") == {"name": "drawing"}


def test_main_treats_a_bare_title_argument_as_the_part_name(capsys, monkeypatch) -> None:
    seen: dict = {}

    def fake_action(args):
        seen["title"] = args.title
        cli.ok(action=args.action)

    monkeypatch.setitem(cli.ACTIONS, "draw", fake_action)
    with pytest.raises(SystemExit):
        cli.main(["draw", "--title", "FLANGE"])
    assert seen["title"] == {"name": "FLANGE"}, (
        "--title FLANGE is a name, not malformed JSON"
    )


def test_main_still_reads_a_json_title_argument(capsys, monkeypatch) -> None:
    seen: dict = {}

    def fake_action(args):
        seen["title"] = args.title
        cli.ok(action=args.action)

    monkeypatch.setitem(cli.ACTIONS, "draw", fake_action)
    with pytest.raises(SystemExit):
        cli.main(["draw", "--title", '{"name": "FLANGE", "rev": "B"}'])
    assert seen["title"] == {"name": "FLANGE", "rev": "B"}


def test_a_drawn_sheet_carries_its_title_and_its_units(tmp_path, capsys) -> None:
    """End to end through ``action_draw``: the shorthand title reaches the DXF.

    Units are asserted because the whole point of the DXF is that a drafter opens
    it: an unset ``$INSUNITS`` leaves every dimension dimensionless.
    """
    with pytest.raises(SystemExit):
        cli.main(
            [
                "draw",
                "--name", "flange",
                "--out-dir", str(tmp_path),
                "--preview", "none",
                "--spec", json.dumps(
                    {
                        "title": "FLANGE",
                        "scale": "1:2",
                        "entities": [{"type": "rect", "p1": [0, 0], "p2": [100, 60]}],
                    }
                ),
            ]
        )
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True, payload
    assert payload["entity_errors"] == []
    assert payload["units_mm"] is True

    written = ezdxf.readfile(tmp_path / "flange.dxf")
    assert written.header["$INSUNITS"] == 4
    texts = [e.dxf.text for e in written.modelspace() if e.dxftype() == "TEXT"]
    assert "FLANGE" in texts
    assert any("SCALE: 1:2" in t for t in texts)


def test_inspect_reports_the_units_code_not_just_the_boolean(tmp_path, capsys) -> None:
    """MEASURED (2026-09-26): a live census printed ``$INSUNITS: None`` for a file
    the engine's own ``inspect`` called ``units_mm: True``.

    Re-measured off the shell: bash had expanded ``$INSUNITS`` to nothing inside
    the census' double-quoted ``python3 -c``, so it asked for the header variable
    named ``""``. The file was always millimetres. The engine reports the raw code
    as well as the boolean so a unitless drawing can never hide behind ``False``.
    """
    with pytest.raises(SystemExit):
        cli.main(
            [
                "draw",
                "--name", "flange",
                "--out-dir", str(tmp_path),
                "--preview", "none",
                "--spec", json.dumps(
                    {
                        "entities": [
                            {"type": "rect", "p1": [0, 0], "p2": [100, 60]},
                            {"type": "dim_linear", "p1": [0, 0], "p2": [100, 0], "axis": "x"},
                        ]
                    }
                ),
            ]
        )
    capsys.readouterr()

    with pytest.raises(SystemExit):
        cli.main(["inspect", "--file", str(tmp_path / "flange.dxf")])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["kind"] == "dxf"
    assert payload["units_code"] == 4
    assert payload["units"] == "millimetres"
    assert payload["units_mm"] is True
    assert payload["dimensions"] == 1
    assert payload["editable_dimensions"] is True
    assert "DIMENSIONS" in payload["layers"]


def test_a_rect_accepts_two_opposite_corners() -> None:
    """The spelling an LLM reaches for first; rejecting it is a pure usability bug."""
    _doc, msp, _w, _h = _msp()
    assert cli._entity_2d(msp, {"type": "rect", "p1": [0, 0], "p2": [100, 60]}) == 1
    polyline = msp.query("LWPOLYLINE")[0]
    points = [(p[0], p[1]) for p in polyline.get_points("xy")]
    assert points == [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)]


def test_a_rect_accepts_corners_in_any_order() -> None:
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "rect", "p1": [100, 60], "p2": [0, 0]})
    polyline = msp.query("LWPOLYLINE")[0]
    assert (polyline.get_points("xy")[0][0], polyline.get_points("xy")[0][1]) == (0.0, 0.0)


def test_a_rect_still_accepts_a_size_and_a_corner() -> None:
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "rect", "corner": [10, 10], "size": [40, 20]})
    points = [(p[0], p[1]) for p in msp.query("LWPOLYLINE")[0].get_points("xy")]
    assert points == [(10.0, 10.0), (50.0, 10.0), (50.0, 30.0), (10.0, 30.0)]


def test_a_degenerate_rect_is_rejected_as_one_entity_not_a_crash() -> None:
    _doc, msp, _w, _h = _msp()
    with pytest.raises(cli.EntityError):
        cli._entity_2d(msp, {"type": "rect", "p1": [5, 5], "p2": [5, 5]})


def test_one_bad_entity_does_not_lose_the_rest_of_the_sheet(capsys, tmp_path) -> None:
    """The regression that mattered: a good drawing with one bad dictionary used to
    produce nothing but an error."""
    spec = {
        "title": {"name": "MIXED"},
        "entities": [
            {"type": "rect", "p1": [0, 0], "p2": [50, 30]},
            {"type": "nonsense", "whatever": 1},
            {"type": "circle", "center": [25, 15], "radius": 5},
        ],
    }
    args = cli.build_parser().parse_args(
        ["draw", "--name", "mixed", "--spec", json.dumps(spec), "--out-dir", str(tmp_path)]
    )
    args.spec = spec
    args.preview = ["none"]
    args.at = None
    with pytest.raises(SystemExit) as exit_info:
        cli.action_draw(args)
    assert exit_info.value.code == 0, "a sheet with one bad entity is still a success"
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["entities_drawn"] == 2
    assert len(payload["entity_errors"]) == 1
    assert "nonsense" in payload["entity_errors"][0]


def test_a_centerline_takes_two_points_or_a_centre() -> None:
    _doc, msp, _w, _h = _msp()
    assert cli._entity_2d(msp, {"type": "centerline", "p1": [0, 0], "p2": [0, 50]}) == 1
    assert cli._entity_2d(msp, {"type": "centerline", "center": [10, 10]}) == 2
    assert all(e.dxf.layer == "CENTER" for e in msp)


def test_a_centerline_with_neither_is_rejected() -> None:
    _doc, msp, _w, _h = _msp()
    with pytest.raises(cli.EntityError):
        cli._entity_2d(msp, {"type": "centerline"})


def test_text_accepts_a_string_alignment() -> None:
    """``set_placement`` asserts on a plain string; it wants the enum."""
    _doc, msp, _w, _h = _msp()
    assert cli._entity_2d(msp, {"type": "text", "at": [10, 10], "text": "NOTE", "align": "LEFT"}) == 1
    assert msp.query("TEXT")[0].dxf.text == "NOTE"


# --------------------------------------------------------------------------- #
# Dimensions: the editable-DXF requirement
# --------------------------------------------------------------------------- #
def test_linear_dimensions_are_real_dimension_entities() -> None:
    """Not exploded lines. This is the whole point of the drawing engine."""
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "dim_linear", "p1": [0, 0], "p2": [100, 0], "axis": "x"})
    cli._entity_2d(msp, {"type": "dim_linear", "p1": [0, 0], "p2": [0, 60], "axis": "y"})
    cli._entity_2d(msp, {"type": "dim_aligned", "p1": [0, 0], "p2": [30, 40]})
    dims = msp.query("DIMENSION")
    assert len(dims) == 3
    assert all(entity.dxf.layer == "DIMENSIONS" for entity in dims)
    # Every one rendered, i.e. has a text position a reader can place.
    assert all(entity.dxf.text_midpoint is not None for entity in dims)
    types = sorted(entity.dxf.dimtype & 0x0F for entity in dims)
    assert types == [0, 0, 0], "linear / vertical / aligned all report type 0"


def test_radius_and_diameter_dimensions_carry_their_own_types() -> None:
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "dim_radius", "center": [50, 30], "radius": 18, "text": "R18"})
    cli._entity_2d(msp, {"type": "dim_diameter", "center": [50, 30], "radius": 10})
    dims = sorted(msp.query("DIMENSION"), key=lambda e: e.dxf.dimtype & 0x0F)
    assert [entity.dxf.dimtype & 0x0F for entity in dims] == [3, 4], "diameter=3, radius=4"
    assert dims[1].dxf.text == "R18", "an explicit text override must survive"


def test_an_angular_dimension_places_its_arc_not_its_vertex() -> None:
    """ezdxf derives the vertex from where the two legs meet. Handing it the vertex
    as ``base`` makes the arc radius zero and the renderer divides by zero."""
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(
        msp,
        {"type": "dim_angular", "center": [0, 0], "radius": 20, "start_angle": 0, "end_angle": 90},
    )
    dims = msp.query("DIMENSION")
    assert len(dims) == 1
    assert dims[0].dxf.dimtype & 0x0F == 2, "angular"
    assert dims[0].dxf.text_midpoint is not None


def test_an_angular_dimension_with_no_span_is_rejected() -> None:
    _doc, msp, _w, _h = _msp()
    with pytest.raises(cli.EntityError):
        cli._entity_2d(
            msp,
            {"type": "dim_angular", "center": [0, 0], "start_angle": 45, "end_angle": 45},
        )


def test_a_dimension_with_no_axis_is_measured_along_its_longer_leg() -> None:
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "dim_linear", "p1": [0, 0], "p2": [100, 10]})
    midpoint = msp.query("DIMENSION")[0].dxf.text_midpoint
    assert midpoint.x == pytest.approx(50.0, abs=1.0), "measured horizontally"


def test_an_unrenderable_dimension_is_dropped_from_the_preview_only() -> None:
    """A DIMENSION with no text_midpoint raises inside ezdxf's drawing addon and
    would cost the user the whole raster, so ``_render_dxf`` filters it out while
    the DXF keeps the entity."""
    doc, msp, _w, _h = _msp()
    msp.add_linear_dim(base=(50, -12), p1=(0, 0), p2=(100, 0), angle=0, dimstyle="EZDXF")
    # Deliberately NOT rendered: no text_midpoint.
    doctored = msp.query("DIMENSION")[0]
    assert doctored.dxf.text_midpoint is None

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        written = cli._render_dxf(doc, "preview", out, ["png"])
        assert "png" in written, "the preview must survive a broken dimension"
        assert Path(written["png"]).stat().st_size > 0


def test_the_hatch_layer_is_used_for_a_hatch() -> None:
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(
        msp, {"type": "hatch", "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}
    )
    assert msp.query("HATCH")[0].dxf.layer == "HATCH"


def test_an_unknown_entity_type_lists_what_is_supported() -> None:
    _doc, msp, _w, _h = _msp()
    with pytest.raises(cli.EntityError) as info:
        cli._entity_2d(msp, {"type": "spline"})
    assert "dim_linear" in str(info.value)


# --------------------------------------------------------------------------- #
# Argument contract with the host tool
# --------------------------------------------------------------------------- #
def test_the_parser_accepts_every_argument_the_tool_builds() -> None:
    """The host tool constructs this argv; a flag renamed here breaks it silently
    at runtime, in a sandbox, with no test otherwise covering the join."""
    args = cli.build_parser().parse_args(
        [
            "project",
            "--spec", "{}",
            "--code", "result = Box(1,1,1)",
            "--params", "{}",
            "--name", "part",
            "--out-dir", "/tmp/out",
            "--formats", "step,stl,3mf",
            "--preview", "png,pdf",
            "--sheet", "A3",
            "--views", "front,top,right,iso",
            "--title", "{}",
            "--axis", "y",
            "--at", "0",
            "--file", "/tmp/x.step",
        ]
    )
    assert args.action == "project"
    assert args.name == "part"
    # Coercion to float happens in main(), not the parser -- asserting it here
    # would be asserting something the parser never promised.
    assert args.at == "0"
    assert args.file == "/tmp/x.step"


def test_main_coerces_comma_separated_lists_and_json(monkeypatch) -> None:
    seen: dict = {}

    def fake_action(args):
        seen.update(
            formats=args.formats, preview=args.preview, views=args.views,
            spec=args.spec, title=args.title,
        )
        cli.ok(action=args.action)

    monkeypatch.setitem(cli.ACTIONS, "model", fake_action)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "model",
                "--spec", '{"kind": "box"}',
                "--formats", "step,stl",
                "--preview", "png",
                "--views", "front,top",
                "--title", '{"name": "X"}',
            ]
        )
    assert seen["formats"] == ["step", "stl"]
    assert seen["preview"] == ["png"]
    assert seen["views"] == ["front", "top"]
    assert seen["spec"] == {"kind": "box"}
    assert seen["title"] == {"name": "X"}


def test_main_turns_a_bad_at_into_a_named_error(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["section", "--at", "sideways"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "--at" in payload["error"]


def test_a_spec_that_is_not_json_names_the_field(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["model", "--spec", "{not json"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "spec" in str(payload).lower()


def test_an_unexpected_engine_error_never_shows_a_bare_traceback(capsys, monkeypatch) -> None:
    """Whatever breaks, the model must receive JSON with a next step, never a
    traceback it has to interpret."""

    def boom(args):
        raise RuntimeError("something inside the engine broke")

    monkeypatch.setitem(cli.ACTIONS, "doctor", boom)
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "something inside the engine broke" in payload["error"]
    assert "traceback" in payload


def test_every_engine_action_is_wired_up() -> None:
    """A CLI action with no entry point in ACTIONS is unreachable from the tool."""
    assert set(cli.ACTIONS) == {
        "doctor", "freecad", "freecad_gui", "model", "draw", "project", "section",
        "inspect", "export",
    }
    for name, function in cli.ACTIONS.items():
        assert callable(function), name


# --------------------------------------------------------------------------- #
# FreeCAD: the default engine
# --------------------------------------------------------------------------- #
# Every case below is a failure that was reproduced against the FreeCAD the
# sandbox actually gets (Debian 12's `0.20.2+dfsg1-4`) on 2026-09-26.
def test_the_freecad_scripts_are_valid_python() -> None:
    """The driver is a string until FreeCAD executes it.

    A syntax error inside it cannot be caught by importing this module, and it
    surfaces in the sandbox as FreeCAD refusing to run -- which reads like the
    engine is missing rather than like a typo. This caught exactly that.
    """
    import ast

    ast.parse(cli.FREECAD_DRIVER, "<fre".replace("<fre", "<driver>"))


def test_the_gui_preview_photographs_the_display() -> None:
    """The GUI action photographs the sandbox display, not a viewport.

    MEASURED: a script handed to ``freecad script.py`` runs BEFORE the GUI
    application exists, so ``FreeCADGui.ActiveDocument`` is None and there is no
    view to save; and 0.20.2's ``FreeCADGui`` has no ``showMainWindow`` or
    ``exec_`` to pump. What is on the display is the window the live screen panel
    streams, so that is what gets captured.
    """
    import inspect

    body = inspect.getsource(cli._capture_display)
    assert "import" in body and "-window" in body and "root" in body
    assert "DISPLAY" in body
    assert not hasattr(cli, "_FREECAD_VIEW_SCRIPT")
    assert "_capture_display(" in inspect.getsource(cli.action_freecad_gui)


def test_the_driver_exports_dxf_through_a_writer_that_ships() -> None:
    """0.20 has no ``DrawPage.saveSvg``, so the driver must not call one.

    MEASURED: ``page.saveSvg`` raises AttributeError, ``page.PageResult`` does not
    exist, and ``Import.export(..., '.dxf')`` returns OK and writes NOTHING. The
    writers that do exist are ``TechDraw.writeDXFPage`` (the sheet) and Draft's
    ``importDXF.export`` (the raw entities), so those are what the driver names.
    """
    driver = cli.FREECAD_DRIVER
    # The comments here name the calls that DO NOT work, so only the executed
    # lines are checked -- otherwise the explanation of the trap trips the trap's
    # own guard.
    code = "\n".join(
        line for line in driver.splitlines() if not line.lstrip().startswith("#")
    )
    assert "TechDraw.writeDXFPage" in code
    assert "importDXF" in code
    assert ".saveSvg(" not in code, "0.20.2 has no sheet-level SVG writer"
    assert ".savePDF(" not in code, "0.20.2 has no sheet-level PDF writer"


def test_the_driver_composes_a_sheet_from_the_views() -> None:
    """``viewPartAsSvg`` is the only SVG this build produces, so it is used."""
    assert "viewPartAsSvg" in cli.FREECAD_DRIVER
    assert "rsvg-convert" in cli.FREECAD_DRIVER, "the rasteriser is how png/pdf land"


def test_preview_view_reaches_the_drawing_views() -> None:
    """``--preview-view`` has to land somewhere real, or it is a lie.

    It is the exit of a pipeline that is easy to leave half-wired: the driver
    reads ``ED_VIEW``, ``add_page`` has to default to it, and the runner has to
    set it. ``ED_VIEW`` was read into a constant nothing used -- i.e. the flag
    was accepted and did nothing -- which is exactly the bug this pins.
    """
    assert 'os.environ.get("ED_VIEW"' in cli.FREECAD_DRIVER
    # add_page defaults its projection direction to VIEW when the caller names
    # neither `views` nor `direction`.
    assert 'wanted = [str(direction or VIEW).strip().lower()]' in cli.FREECAD_DRIVER
    import inspect

    assert '"ED_VIEW": view or "iso"' in inspect.getsource(cli._run_freecad_driver)
    assert "view=args.preview_view" in inspect.getsource(cli.action_freecad)


def test_add_page_can_put_several_views_on_one_sheet() -> None:
    """One view is not a drawing: shape cannot be read from a single projection.

    `views=[...]` places front/right/top/iso in a third-angle grid on one page,
    and `objects` falls back to the design's result solids rather than requiring
    the caller to name them (which is how the scaffolding got sheeted before).
    """
    driver = cli.FREECAD_DRIVER
    assert "views=None" in driver
    assert "slots = [(85.0, 62.0), (212.0, 62.0), (85.0, 145.0), (212.0, 145.0)]" in driver
    assert "or _results()" in driver


def test_the_design_is_measured_from_its_result_solids_not_its_scaffolding() -> None:
    """MEASURED DEFECT: a plate+cylinder Part::Cut reported 362 021 mm3.

    The part was 47 360 mm3. Every primitive behind a boolean carries a non-null
    Shape, so "objects with a shape" is not the part -- it also made the STEP
    export contain all 14 solids. The result set is the shapes minus the ones
    another object consumes as Base/Tool/Shapes.
    """
    driver = cli.FREECAD_DRIVER
    assert "def _consumed()" in driver
    assert "def _results()" in driver
    assert 'for prop in ("Base", "Tool", "Shapes", "Source", "Objects", "BaseFeature")' in driver
    assert "objs = _results()" in driver
    assert "_measure_results(objs)" in driver
    # The scaffolding is reported, just not measured or exported as the part.
    assert 'RESULT["construction_objects"] = sorted(_consumed())' in driver


def test_a_drawing_view_is_not_treated_as_consuming_the_part() -> None:
    """MEASURED DEFECT: add_page() made the result set vanish, so the scaffolding came back.

    A `TechDraw::DrawViewPart` carries the part body in its `Source`, exactly like
    a `Part::Cut` carries its base in `Base`. Counting it as a consumer swallowed
    the finished body too, `_results()` came back empty, and the driver fell back
    to "every object with a shape" -- the 359 705 mm3 / 14-object answer, with a
    sheet on the page. A view is a view of the part, not a consumer of it, so
    drawing objects are skipped when the consumed set is built. Live: the same
    chain measures 47 360.62 mm3 with and without a page.
    """
    driver = cli.FREECAD_DRIVER
    assert 'if str(getattr(obj, "TypeId", "")).startswith("TechDraw::"):' in driver


def test_the_sheet_has_white_paper_behind_it() -> None:
    """MEASURED: FreeCAD's A4 template fills nothing, so the rasterised sheet was
    transparent background with near-black lines -- a blank image to any viewer
    that is not white. Live: the exported PNG carried mean alpha 10.8 and
    RGB 2.0 where it was opaque, and composited on white it is a real drawing."""
    driver = cli.FREECAD_DRIVER
    assert "fill=\"#ffffff\"" in driver
    assert "text[:opened + 1] + paper + text[opened + 1:]" in driver
    assert 'r\'width="([0-9.]+)mm"\'' in driver


def test_the_gui_photo_waits_for_the_window_to_stop_changing() -> None:
    """MEASURED: `view_png` came back as FreeCAD's splash screen.

    A 106 011-byte frame, 93% black, luminance spread 28.6 -- handed to the user
    as their drawing. No byte threshold separates a splash screen from a window,
    so the capture now keeps the first frame that passes the size test and only
    returns once two consecutive frames share ImageMagick's signature: motion is
    what says the document is still opening. A window that never settles still
    yields its best frame rather than an error.
    """
    source = Path(cli.__file__).read_text()
    assert "def _frame_signature(path: Path) -> str:" in source
    assert '["identify", "-format", "%#", str(path)]' in source
    assert "if signature and signature == previous:" in source
    assert "if previous:" in source


def test_asking_for_a_drawing_without_a_sheet_still_produces_one() -> None:
    """MEASURED: `--formats step,dxf,svg,pdf` with no add_page() gave one DXF and
    three "no TechDraw sheet to render" errors. Asking for svg/pdf/png IS asking
    for a sheet, so the driver builds one from the result solids and says so."""
    driver = cli.FREECAD_DRIVER
    assert 'AUTO_SHEET = os.environ.get("ED_AUTO_SHEET", "1") not in ("", "0")' in driver
    assert 'SHEET_VIEWS = [v for v in (os.environ.get("ED_VIEWS") or "").split(",")' in driver
    assert 'any(f in WANTED for f in ("svg", "png", "pdf"))' in driver
    assert "add_page(views=SHEET_VIEWS or" in driver
    # `--views` is how the caller picks the projections without writing add_page().
    source = Path(cli.__file__).read_text() if hasattr(cli, "__file__") else ""
    assert '"ED_VIEWS": ",".join(views or [])' in source
    assert "views=args.views or []" in source


def test_the_gui_action_does_not_sheet_the_document_behind_the_models_back() -> None:
    """`freecad_gui` opens a window; it is not a drawing request.

    It asks for an incidental `svg`, which under the auto-sheet rule would build a
    TechDraw page nobody asked for and change what the window shows. A sheet on
    the GUI path is the model's call, through add_page() in its own snippet.
    """
    source = Path(cli.__file__).read_text()
    assert "auto_sheet: bool = True" in source
    assert "            auto_sheet=False," in source


def test_preview_png_on_the_headless_action_asks_for_a_png() -> None:
    """``--preview png`` used to be accepted on `freecad` and do nothing.

    The headless action reads ``--formats`` for what to write, so a picture asked
    for with ``--preview`` has to be folded into that list; otherwise the model
    asks for a PNG, gets ``ok: true``, and no PNG.
    """
    import inspect

    body = inspect.getsource(cli.action_freecad)
    assert "for extra in args.preview or []" in body
    assert "formats.append(fmt)" in body


def test_the_freecad_environment_pins_the_debian_interpreter() -> None:
    """The whole engine depends on this pair; a regression here is a silent death.

    Without both, FreeCAD's embedded python dies on ``import math`` and the model
    sees "FreeCAD is broken" instead of "this env var is missing".
    """
    assert cli.FREECAD_ENV["PYTHONHOME"] == "/usr"
    assert "/usr/lib/x86_64-linux-gnu" in cli.FREECAD_ENV["LD_LIBRARY_PATH"]


def test_freecad_which_finds_the_launcher_even_without_a_path_entry(monkeypatch) -> None:
    """A sandbox rc file may not export /usr/bin, so the fallback path matters."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.os.path, "isfile", lambda path: path == "/usr/bin/freecadcmd")
    monkeypatch.setattr(cli.os, "access", lambda path, mode: True)
    assert cli._freecad_which() == "/usr/bin/freecadcmd"
    monkeypatch.setattr(cli.os.path, "isfile", lambda path: path == "/usr/bin/freecad")
    assert cli._freecad_which(gui=True) == "/usr/bin/freecad"


def test_the_display_can_be_pointed_elsewhere(monkeypatch) -> None:
    """The live screen panel reads its display from the environment, so this must too."""
    monkeypatch.setenv(cli.DISPLAY_ENV, ":7")
    assert cli._display_name() == ":7"
    monkeypatch.delenv(cli.DISPLAY_ENV, raising=False)
    assert cli._display_name() == ":99"


def test_doctor_names_freecad_as_the_preferred_engine(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_freecad_which", lambda gui=False: "/usr/bin/freecadcmd")
    monkeypatch.setattr(cli, "_freecad_version", lambda binary: "0.20.2")
    monkeypatch.setitem(cli.ACTIONS, "doctor", cli.ACTIONS["doctor"])
    with pytest.raises(SystemExit):
        cli.action_doctor(type("A", (), {"json": False, "out_dir": None,
                                        "name": None, "sheet": None,
                                        "spec": None, "code": None,
                                        "formats": None, "preview": None,
                                        "views": None, "title": None,
                                        "axis": None, "at": None, "file": None,
                                        "timeout": None, "preview_view": None})())
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["preferred_engine"] == "freecad"
    assert payload["freecad"]["version"] == "0.20.2"
    assert payload["capabilities"]["live_screen_cad_window"] is True


def test_doctor_falls_back_when_freecad_is_absent(capsys, monkeypatch) -> None:
    """An image without FreeCAD must still be usable, and must say so."""
    monkeypatch.setattr(cli, "_freecad_which", lambda gui=False: None)
    with pytest.raises(SystemExit):
        cli.action_doctor(type("A", (), {"json": False, "out_dir": None,
                                        "name": None, "sheet": None,
                                        "spec": None, "code": None,
                                        "formats": None, "preview": None,
                                        "views": None, "title": None,
                                        "axis": None, "at": None, "file": None,
                                        "timeout": None, "preview_view": None})())
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["preferred_engine"] == "build123d"
    assert payload["freecad"]["available"] is False
    assert payload["capabilities"]["live_screen_cad_window"] is False


def test_freecad_refuses_to_run_before_it_is_installed(capsys, monkeypatch) -> None:
    """The install is the only thing that puts FreeCAD there, so say that."""
    monkeypatch.setattr(cli, "_freecad_which", lambda gui=False: None)
    with pytest.raises(SystemExit):
        cli.action_freecad(type("A", (), {"out_dir": None, "name": "x", "code": "pass",
                                        "file": None, "formats": None,
                                        "preview_view": None, "preview": None,
                                        "views": None, "timeout": None})())
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "install" in payload["next"]


def test_freecad_gui_says_the_gui_is_missing_when_it_is(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_freecad_which", lambda gui=False: None)
    with pytest.raises(SystemExit):
        cli.action_freecad_gui(type("A", (), {"out_dir": None, "name": "x", "code": None,
                                              "file": None, "preview": None,
                                              "preview_view": None, "timeout": None})())
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "GUI" in payload["error"]


# --------------------------------------------------------------------------- #
# The sandbox installer
# --------------------------------------------------------------------------- #
# FreeCAD is installed by the installer and nowhere else -- the model is never
# allowed to use an engine that is not on the box yet -- so the installer is the
# thing that has to be right.
INSTALLER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_engineering_draw.sh"


def _installer_text() -> str:
    return INSTALLER_PATH.read_text()


def test_the_installer_installs_freecad() -> None:
    text = _installer_text()
    assert "freecad freecad-python3" in text, (
        "FreeCAD is the default engine, so the installer is what must put it there"
    )
    assert "xvfb" in text, "a GUI engine needs a display, and that is what the live screen captures"
    assert "matchbox-window-manager" in text, (
        "a bare Xvfb leaves the window unmapped and the screen capture records an empty desktop"
    )
    assert "librsvg2-bin" in text, "rsvg-convert is how a TechDraw sheet becomes a PNG"


def test_the_installer_exports_the_environment_freecad_needs() -> None:
    text = _installer_text()
    assert "export PYTHONHOME=/usr" in text
    assert "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu" in text
    assert "export DISPLAY=:99" in text


def test_the_installer_proves_freecad_builds_a_solid() -> None:
    """Starting is not working: the smoke test cuts a solid and exports it."""
    text = _installer_text()
    assert "ed_freecad_smoke" in text
    assert "Part.export" in text
    assert "solids" in text, "a startable FreeCAD that produces no solid is not a working engine"


def test_the_installer_accepts_a_freecad_only_sandbox() -> None:
    """FreeCAD alone must be enough to call the install a success.

    It is the preferred engine; requiring build123d on top of it would leave a
    sandbox where FreeCAD works reporting itself broken.
    """
    text = _installer_text()
    assert '[ "$freecad_ready" = "yes" ]' in text


def test_the_installer_writes_what_landed() -> None:
    text = _installer_text()
    assert "install.json" in text
    assert "freecad.status" in text


def test_the_installer_is_valid_bash() -> None:
    import subprocess

    done = subprocess.run(["bash", "-n", str(INSTALLER_PATH)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# --------------------------------------------------------------------------- #
# Breadth: the 2D sheet has to accept a drawing described the way a model
# describes it, which is what the live 26-entity A3 stress sheet showed.
# --------------------------------------------------------------------------- #
def test_a_line_accepts_the_p1_p2_spelling_the_dimensions_use() -> None:
    """MEASURED FAILURE (2026-09-26, live sandbox, A3 sheet): a line written as
    ``{"type": "line", "p1": [0, 0], "p2": [100, 0]}`` came back
    ``entity #12 (line): line is missing 'start'`` -- while the *same response*,
    in its own ``note`` field, told the caller to write lines as
    ``p1=[0,0], p2=[100,0]``. Every dimension type takes p1/p2 and the rect branch
    already accepted four spellings; the line branch was the one that did not.
    """
    _doc, msp, _w, _h = _msp()
    assert cli._entity_2d(msp, {"type": "line", "p1": [0, 0], "p2": [100, 0]}) == 1
    assert cli._entity_2d(msp, {"type": "line", "from": [0, 10], "to": [100, 10]}) == 1
    assert cli._entity_2d(msp, {"type": "line", "start": [0, 20], "end": [100, 20]}) == 1
    lines = msp.query("LINE")
    assert len(lines) == 3
    assert (lines[0].dxf.start.x, lines[0].dxf.end.x) == (0.0, 100.0)


def test_a_line_with_only_one_end_is_still_refused() -> None:
    """Accepting more spellings must not turn a half-specified line into a dot."""
    _doc, msp, _w, _h = _msp()
    with pytest.raises(cli.EntityError):
        cli._entity_2d(msp, {"type": "line", "p1": [0, 0]})


def test_a_polyline_honours_close_as_well_as_closed() -> None:
    """MEASURED FAILURE (live, identical spec): ``"close": true`` on a three-point
    polyline wrote ``LWPOLYLINE closed = False points = 3`` -- a closed triangle
    silently became an open two-segment path. Only ``"closed"`` was read, and the
    difference is geometry, not a warning."""
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "polyline", "points": [[0, 0], [10, 0], [10, 10]], "close": True})
    cli._entity_2d(
        msp, {"type": "polyline", "points": [[20, 0], [30, 0], [30, 10]], "closed": True}
    )
    cli._entity_2d(msp, {"type": "polyline", "points": [[40, 0], [50, 0], [50, 10]]})
    polys = msp.query("LWPOLYLINE")
    assert [p.closed for p in polys] == [True, True, False], "close and closed both mean closed"


def test_text_and_hatch_keep_the_layer_they_were_asked_for() -> None:
    """MEASURED FAILURE (live): the text branch hard-coded
    ``dxfattribs={"layer": "ANNOTATION"}`` and the hatch branch hard-coded
    ``{"layer": "HATCH"}``, so a text asked onto HIDDEN landed on ANNOTATION and a
    hatch asked onto HIDDEN landed on HATCH. ``SKILL.md`` promises
    ``override with "layer": "HIDDEN"``, and the layer decides colour, linetype and
    lineweight -- a hatch on the wrong layer is the wrong drawing."""
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "text", "at": [10, 10], "text": "NOTE", "layer": "HIDDEN"})
    cli._entity_2d(
        msp,
        {"type": "hatch", "points": [[0, 0], [10, 0], [10, 10]], "layer": "HIDDEN"},
    )
    cli._entity_2d(msp, {"type": "text", "at": [20, 20], "text": "PLAIN"})
    assert [e.dxf.layer for e in msp.query("TEXT")] == ["HIDDEN", "ANNOTATION"]
    assert [e.dxf.layer for e in msp.query("HATCH")] == ["HIDDEN"]


def test_a_hatch_with_no_boundary_is_refused_rather_than_drawn_empty() -> None:
    """MEASURED FAILURE (live): a hatch entity with no ``points`` took a fallback
    whose comment claimed it "fills the entities already drawn, which is what a
    drafter would do". It never did -- it called ``add_hatch().set_solid_fill()``
    with no path, which writes a HATCH with zero boundary paths, renders as
    nothing, and reports success. A shapeless entity in the user's DXF plus a
    "drawn" tally is worse than a named error."""
    _doc, msp, _w, _h = _msp()
    with pytest.raises(cli.EntityError) as info:
        cli._entity_2d(msp, {"type": "hatch"})
    assert "boundary" in str(info.value)
    assert len(msp.query("HATCH")) == 0, "nothing shapeless may reach the DXF"


def test_a_dimension_placed_with_at_lands_on_the_line_that_was_asked_for() -> None:
    """MEASURED FAILURE (live): the linear-dimension branch read only ``offset``,
    so the natural ``at=[120, 8]`` -- the same key a text entity uses for its
    position -- was silently dropped and every dimension fell back to the default
    -12 mm. Dimensions that land where nobody asked for them are the ones the user
    then measures by hand."""
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(msp, {"type": "dim_linear", "p1": [20, 20], "p2": [220, 20], "at": [120, 8]})
    cli._entity_2d(
        msp, {"type": "dim_vertical", "p1": [220, 20], "p2": [220, 140], "at": [238, 80]}
    )
    cli._entity_2d(msp, {"type": "dim_aligned", "p1": [30, 120], "p2": [70, 120], "at": [50, 128]})
    dims = msp.query("DIMENSION")
    assert len(dims) == 3
    # defpoint is where the dimension line starts, so it carries the offset asked for.
    assert dims[0].dxf.defpoint.y == pytest.approx(8.0), "horizontal dim on y=8"
    assert dims[1].dxf.defpoint.x == pytest.approx(238.0), "vertical dim on x=238"
    assert dims[2].dxf.defpoint.y == pytest.approx(128.0), "aligned dim offset 8 mm in +y"


def test_an_explicit_offset_still_wins_over_at() -> None:
    _doc, msp, _w, _h = _msp()
    cli._entity_2d(
        msp,
        {"type": "dim_linear", "p1": [0, 0], "p2": [100, 0], "at": [50, 5], "offset": -30.0},
    )
    assert msp.query("DIMENSION")[0].dxf.defpoint.y == pytest.approx(-30.0)


def test_the_raster_preview_puts_black_on_white_so_colour_7_is_readable() -> None:
    """MEASURED FAILURE (live sandbox, A3 sheet, 26 entities): the whole PNG had
    **0 dark pixels**. ``ezdxf`` resolves AutoCAD colour 7 -- "white or black,
    whichever the paper is not" -- against the drawing *background policy*, and
    ``Configuration()``'s default resolves it to **white**. OUTLINE, BORDER and
    TITLE are all colour 7, so the frame, the title block, every outline, 2 of the
    6 dimension types and 24 of the 37 entities were painted white on white and
    were absent from every PNG, PDF and SVG the engine had ever produced.

    Before/after on the identical spec: png 22 635 B, mean 254.78, dark<128 **0**
    -> png 84 089 B, mean 248.82, dark<128 **25 246**.
    """
    import tempfile

    doc, msp, width, height = _msp()
    cli._border_and_title(msp, width, height, {"name": "CONTRAST", "material": "STEEL"})
    # One line per layer, so the ink can be attributed to a colour.
    for name in ("OUTLINE", "BORDER", "TITLE"):
        assert cli.LAYERS[name]["color"] == 7, "colour 7 is the one that flips with the paper"
    msp.add_line((20, 20), (200, 20), dxfattribs={"layer": "OUTLINE"})
    msp.add_line((20, 40), (200, 40), dxfattribs={"layer": "BORDER"})
    msp.add_line((20, 60), (200, 60), dxfattribs={"layer": "TITLE"})

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        cli._render_dxf(doc, "contrast", out, ["png"])
        png = out / "contrast.png"
        assert png.is_file(), "the preview must exist"
        from PIL import Image
        import numpy as np

        array = np.asarray(Image.open(png).convert("L"))
        dark = int((array < 128).sum())
    assert dark > 2000, f"the frame and title block must be visible; got {dark} dark pixels"

    source = Path(cli.__file__).read_text()
    assert "BackgroundPolicy.WHITE" in source
    assert "config=Configuration(background_policy=BackgroundPolicy.WHITE)" in source


def test_a_whole_sheet_of_every_entity_type_draws_with_no_errors() -> None:
    """The live stress sheet, as a test: every 2D entity type the engine advertises
    on one A3 page, and nothing is allowed to fall over. The live run reported
    ``entities_drawn: 26, entity_errors: []``."""
    _doc, msp, _w, _h = _msp()
    entities = [
        {"type": "rect", "p1": [20, 20], "p2": [220, 140]},
        {"type": "circle", "center": [120, 80], "radius": 45},
        {"type": "circle", "center": [120, 80], "radius": 12, "layer": "HIDDEN"},
        {"type": "circle", "center": [120, 80], "radius": 22, "count": 6, "bolt_circle_radius": 33},
        {"type": "centerline", "p1": [70, 80], "p2": [170, 80]},
        {"type": "centerline", "center": [10, 10]},
        {"type": "arc", "center": [120, 80], "radius": 60, "start_angle": 20, "end_angle": 130},
        {"type": "ellipse", "center": [195, 110], "major_radius": 26, "minor_radius": 14},
        {"type": "polyline", "points": [[30, 120], [50, 135], [70, 120]], "close": True},
        {"type": "hatch", "points": [[30, 30], [70, 30], [70, 55], [30, 55]]},
        {"type": "text", "text": "SECTION A-A", "at": [26, 148], "height": 5},
        {"type": "line", "p1": [0, 0], "p2": [10, 10], "layer": "CONSTRUCTION"},
        {"type": "dim_linear", "p1": [20, 20], "p2": [220, 20], "at": [120, 8]},
        {"type": "dim_vertical", "p1": [220, 20], "p2": [220, 140], "at": [238, 80]},
        {"type": "dim_aligned", "p1": [30, 120], "p2": [70, 120], "at": [50, 128]},
        {"type": "dim_radius", "center": [120, 80], "radius": 45, "angle": 150},
        {"type": "dim_diameter", "center": [120, 80], "diameter": 24},
        {"type": "dim_angular", "center": [120, 80], "radius": 70, "start_angle": 20,
         "end_angle": 130},
    ]
    drawn = 0
    for entity in entities:
        drawn += cli._entity_2d(msp, entity)
    assert drawn >= len(entities) - 1, "the bolt circle adds four extra circles"
    assert len(msp.query("DIMENSION")) == 6
    for dimension in msp.query("DIMENSION"):
        assert dimension.dxf.text_midpoint is not None, "every dimension must be renderable"
