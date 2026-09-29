"""Regression tests for two live-reported "MT5 is failing" bugs in ``mt5_cli.py``.

Both were measured on 2026-09-29 in a real Freestyle sandbox (Ubuntu 24.04, Wine
10.0, MT5 build 6231, a Deriv-Demo account) while tracing why an agent could not
get a balance out of the stack. Neither is a Wine, apt, or installer problem --
the install was healthy throughout -- yet both present as one:

1. **``--text`` before the subcommand disabled the Wine re-exec.**
   The re-exec read ``argv[0]`` as the action, and ``--text`` is a top-level flag
   that argparse happily accepts first, so the action was never recognised. The
   call ran on the *Linux* python and answered with

       mt5_cli.py must run inside Wine to reach the MT5 bridge. It should re-exec
       automatically; run it via mt5_cli.py (not directly) or set MT5_UNDER_WINE=1.

   which is unactionable on every clause and reads as a broken install.

2. **``login`` could not supply credentials on its own.**
   ``cmd_login`` runs under Wine, so it can only ``mt5.initialize()`` and
   ``mt5.login()``. A terminal booted without ``/config:`` has no account, so
   ``initialize()`` never completes the IPC handshake and dies on the ~240 s IPC
   timeout with "Run start first." -- and the bare ``start`` that message asks for
   produces another account-less terminal. The terminal log shows the real tell:
   startup lines and not one ``Network`` line.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mt5_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("mt5_cli_dispatch_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli()


# --------------------------------------------------------------------------- #
# 1. the global flag must not consume the action
# --------------------------------------------------------------------------- #

def test_action_of_skips_the_global_text_flag(cli):
    assert cli._action_of(["--text", "login", "--login", "41261482"]) == "login"


def test_action_of_reads_a_bare_action(cli):
    assert cli._action_of(["account"]) == "account"


def test_action_of_reads_an_action_after_help(cli):
    assert cli._action_of(["--help", "positions"]) == "positions"


def test_action_of_returns_none_when_there_is_no_action(cli):
    assert cli._action_of([]) is None
    assert cli._action_of(["--text"]) is None
    assert cli._action_of(["--text", "-h"]) is None


def _reexec(cli, monkeypatch, tmp_path, argv, *, bootstrap=None):
    """Drive ``_reexec_under_wine`` over a faked, credentialed Wine install.

    ``subprocess.run`` is a no-op and therefore writes no stdout capture, so a
    call that gets far enough to try returns ``fail(...)``'s code. The point of
    the helper is the ``None`` / non-``None`` split: ``None`` means "no re-exec
    needed", which for a bridge action on Linux is the bug.

    ``bootstrap`` replaces the credentialed-launch step, which would otherwise
    touch the real filesystem; pass a recorder to assert on when it ran.
    """
    monkeypatch.setattr(cli, "under_wine", lambda: False)
    monkeypatch.setattr(cli, "win_python", lambda: tmp_path / "Python311" / "python.exe")
    monkeypatch.setattr(cli, "WINE_PREFIX", tmp_path)
    monkeypatch.setattr(cli, "wine_bin", lambda: "wine")
    monkeypatch.setattr(cli, "wine_env", lambda: {})
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "_bootstrap_credentialed_terminal", bootstrap or (lambda argv: None)
    )
    (tmp_path / "drive_c").mkdir(exist_ok=True)
    return cli._reexec_under_wine(list(argv))


def test_text_before_the_subcommand_still_re_execs(cli, monkeypatch, tmp_path, capsys):
    """THE BUG: this used to return None and run the action on Linux python."""
    code = _reexec(
        cli,
        monkeypatch,
        tmp_path,
        ["--text", "login", "--login", "41261482", "--password", "x", "--server", "Deriv-Demo"],
    )
    capsys.readouterr()
    assert code is not None, (
        "--text before the subcommand skipped the Wine re-exec, so the caller got "
        "the 'must run inside Wine' refusal that reads as a broken install"
    )


def test_a_bare_bridge_action_still_re_execs(cli, monkeypatch, tmp_path, capsys):
    code = _reexec(cli, monkeypatch, tmp_path, ["account"])
    capsys.readouterr()
    assert code is not None


def test_a_non_bridge_action_never_re_execs(cli, monkeypatch, tmp_path, capsys):
    """``doctor`` runs natively on Linux by design -- it must stay that way."""
    for argv in (["doctor"], ["--text", "doctor"], ["status"], ["install"]):
        assert _reexec(cli, monkeypatch, tmp_path, argv) is None, argv
    capsys.readouterr()


def test_text_before_a_bridge_action_is_not_reported_as_a_broken_install(
    cli, monkeypatch, tmp_path, capsys
):
    """The exact unactionable sentence must never reach a caller again."""
    _reexec(
        cli,
        monkeypatch,
        tmp_path,
        ["--text", "positions"],
    )
    captured = capsys.readouterr()
    assert "must run inside Wine" not in captured.out
    assert "must run inside Wine" not in captured.err


# --------------------------------------------------------------------------- #
# 2. login must bring up a terminal that actually carries the account
# --------------------------------------------------------------------------- #

def _bootstrap(cli, monkeypatch, argv, *, credentialed=False, live=False):
    """Run the bootstrap over a faked sandbox and report what ``cmd_start`` saw."""
    calls: list[object] = []

    monkeypatch.setattr(cli, "broker_for_server", lambda server: {"key": "deriv"})
    monkeypatch.setattr(cli, "find_terminal", lambda prefer_key=None: Path("/t/terminal64.exe"))
    monkeypatch.setattr(cli, "_terminal_has_credentials", lambda terminal=None: credentialed)
    monkeypatch.setattr(
        cli,
        "_bridge_probe",
        lambda timeout=20: {"ok": True, "account": {"login": 41261482}} if live else None,
    )

    def fake_start(args):
        calls.append(args)
        print('{"ok": true, "swallowed": true}')
        return 0

    monkeypatch.setattr(cli, "cmd_start", fake_start)
    cli._bootstrap_credentialed_terminal(list(argv))
    return calls


def test_login_launches_a_credentialed_terminal(cli, monkeypatch):
    """THE BUG: without this, ``login`` alone can never authorize anything."""
    calls = _bootstrap(
        cli,
        monkeypatch,
        ["login", "--login", "41261482", "--password", "pw", "--server", "Deriv-Demo"],
    )
    assert len(calls) == 1, "login must launch the terminal with /config: credentials"
    assert calls[0].login == "41261482"
    assert calls[0].password == "pw"
    assert calls[0].server == "Deriv-Demo"
    assert calls[0].portable is False


def test_login_accepts_the_equals_form_of_the_flags(cli, monkeypatch):
    calls = _bootstrap(
        cli,
        monkeypatch,
        ["--text", "login", "--login=41261482", "--password=pw", "--server=Deriv-Demo"],
    )
    assert len(calls) == 1
    assert calls[0].server == "Deriv-Demo"


def test_login_without_credentials_does_not_launch_anything(cli, monkeypatch):
    """A bare ``login`` is a read of the current account, not a request to boot one."""
    assert _bootstrap(cli, monkeypatch, ["login"]) == []
    assert _bootstrap(cli, monkeypatch, ["login", "--login", "41261482"]) == []


def test_a_live_credentialed_terminal_is_not_restarted(cli, monkeypatch):
    calls = _bootstrap(
        cli,
        monkeypatch,
        ["login", "--login", "41261482", "--password", "pw", "--server", "Deriv-Demo"],
        credentialed=True,
        live=True,
    )
    assert calls == [], "a terminal that is already up and authorized must be reused"


def test_a_credentialed_terminal_with_no_account_is_restarted(cli, monkeypatch):
    calls = _bootstrap(
        cli,
        monkeypatch,
        ["login", "--login", "41261482", "--password", "pw", "--server", "Deriv-Demo"],
        credentialed=True,
        live=False,
    )
    assert len(calls) == 1


def test_the_launch_payload_never_reaches_stdout(cli, monkeypatch, capsys):
    """Two JSON objects on one stdout is unparseable, which is the whole failure mode."""
    _bootstrap(
        cli,
        monkeypatch,
        ["login", "--login", "41261482", "--password", "pw", "--server", "Deriv-Demo"],
    )
    captured = capsys.readouterr()
    assert "swallowed" not in captured.out
    assert captured.out.strip() == ""


def test_an_unresolvable_terminal_is_not_launched(cli, monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(cli, "broker_for_server", lambda server: {"key": "deriv"})
    monkeypatch.setattr(cli, "find_terminal", lambda prefer_key=None: None)
    monkeypatch.setattr(cli, "cmd_start", lambda args: calls.append(args) or 0)
    cli._bootstrap_credentialed_terminal(
        ["login", "--login", "41261482", "--password", "pw", "--server", "Deriv-Demo"]
    )
    assert calls == [], "preflight owns the 'no terminal installed' refusal"


def test_reexec_bootstraps_before_re_exec_ing_login(cli, monkeypatch, tmp_path, capsys):
    """The bootstrap has to run on the Linux side -- a Wine process cannot launch wine."""
    seen: list[list[str]] = []
    argv = ["--text", "login", "--login", "41261482", "--password", "pw", "--server", "Deriv-Demo"]
    _reexec(cli, monkeypatch, tmp_path, argv, bootstrap=lambda a: seen.append(list(a)))
    capsys.readouterr()
    assert seen == [argv]


def test_reexec_does_not_bootstrap_non_login_actions(cli, monkeypatch, tmp_path, capsys):
    seen: list[list[str]] = []
    for argv in (["account"], ["positions"], ["doctor"]):
        _reexec(cli, monkeypatch, tmp_path, argv, bootstrap=lambda a: seen.append(list(a)))
    capsys.readouterr()
    assert seen == [], "only login owns the credentialed launch"


def test_argv_flag_reads_both_forms(cli):
    assert cli._argv_flag(["--login", "1"], "--login") == "1"
    assert cli._argv_flag(["--login=1"], "--login") == "1"
    assert cli._argv_flag([], "--login") is None
    assert cli._argv_flag(["--login"], "--login") is None
    # A value that merely resembles the flag must not be mistaken for one.
    assert cli._argv_flag(["--password", "--login"], "--login") is None
