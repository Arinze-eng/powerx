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
        "doctor", "model", "draw", "project", "section", "inspect", "export",
    }
    for name, function in cli.ACTIONS.items():
        assert callable(function), name
