"""Regression tests for the *bridge-missing* refusal in ``scripts/mt5_cli.py``.

The bug: ``require_bridge()`` answered a sandbox whose chain was present but
whose ``MetaTrader5`` bridge would not import with a sentence written for a
HUMAN --

    MetaTrader5 bridge is not installed. Run: mt5_cli.py install

-- and, crucially, it carried no ``stage``. ``mt5_sandbox`` only auto-provisions
when it sees ``stage="not_installed"`` (that is how ``require_installed_chain``
already reports a missing chain), so this payload was passed through untouched
and the model relayed the text to the user as an instruction:

    "The MetaTrader 5 bridge is not installed. Please run `mt5_cli.py install`
     to install it."

Installing is the tool's job, never the user's. These tests pin the contract that
stops that reply: a missing bridge is a ``not_installed`` refusal, it names what
is missing, and it never tells anyone to install anything.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mt5_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("mt5_cli_bridge_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli()


def _refusal(cli, monkeypatch, capsys, *, missing: list[str]):
    """Run ``require_bridge()`` over a faked bridge-less, chain-complete sandbox."""
    monkeypatch.setattr(cli, "mt5_module", lambda: None)
    # Inside Wine, so the "must run inside Wine" branch is not the one taken.
    monkeypatch.setattr(cli, "under_wine", lambda: True)
    monkeypatch.setattr(
        cli,
        "installed_chain",
        lambda: {"installed": not missing, "missing": list(missing)},
    )

    mt5, code = cli.require_bridge()
    captured = capsys.readouterr()
    assert mt5 is None, "a missing bridge must not return a usable module"
    # The CLI prints its JSON on stdout as the last line; stderr carries the text.
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return payload, code, captured


def test_missing_bridge_is_a_not_installed_refusal(cli, monkeypatch, capsys):
    """The modelled cause: chain on disk, bridge not importable."""
    payload, code, _ = _refusal(cli, monkeypatch, capsys, missing=[])

    assert payload["ok"] is False
    assert payload["stage"] == "not_installed", (
        "the tool keys auto-provisioning off stage == 'not_installed'; without it "
        "this payload is handed to the model verbatim"
    )
    assert payload["missing"] == ["bridge"], (
        "the chain can be complete while the bridge cannot import, so the refusal "
        "must name the bridge itself rather than an empty list"
    )
    assert payload["next"], "a refusal must say which call to make next"
    assert code != 0, "a refusal is not a success"


def test_missing_bridge_names_the_chain_pieces_from_disk(cli, monkeypatch, capsys):
    """When the chain really is partial, the real missing pieces are reported."""
    payload, _, _ = _refusal(
        cli, monkeypatch, capsys, missing=["wine", "windows_python"]
    )

    assert payload["stage"] == "not_installed"
    assert payload["missing"] == ["wine", "windows_python"]
    assert "wine, windows_python" in payload["error"]


@pytest.mark.parametrize(
    "text",
    [
        # The exact reply the user reported, and the CLI wording it was copied from.
        "please run",
        "run: mt5_cli.py install",
        "mt5_cli.py install",
        "please install",
    ],
)
def test_refusal_never_tells_the_user_to_install(cli, monkeypatch, capsys, text):
    """The literal reply the user complained about must be unreachable."""
    payload, _, captured = _refusal(cli, monkeypatch, capsys, missing=[])

    combined = f"{payload['error']}\n{captured.err}".lower()
    assert text not in combined, (
        f"{text!r} is an instruction for the user; installing is the tool's job"
    )


def test_refusal_points_the_model_at_the_tool_not_the_user(cli, monkeypatch, capsys):
    """The message must redirect the agent, because a bare error gets relayed."""
    payload, _, _ = _refusal(cli, monkeypatch, capsys, missing=[])

    lowered = payload["error"].lower()
    assert "do not ask the user" in lowered
    assert "action='install'" in payload["error"]
