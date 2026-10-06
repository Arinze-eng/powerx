"""Tests for the parts of ``scripts/media_cli.py`` that a sandbox proved wrong.

Two things are covered here, and both are defects measured live rather than
guesses:

1. **YouTube downloads fail with no JavaScript runtime.** MEASURED 2026-10-06 on
   Freestyle with yt-dlp 2026.08.19: ``[debug] JS runtimes: none`` and
   ``WARNING: [youtube] No supported JavaScript runtime could be found``. Without
   a runtime yt-dlp cannot execute YouTube's signature/PO-token challenge, which
   is what surfaces to the user as "Sign in to confirm you're not a bot". The
   fix is a runtime plus a player-client rotation, and both are tested for the
   contract they must not break: an OLD yt-dlp that does not know
   ``--js-runtimes`` must still be handed a command it accepts.

2. **Branding a short needs a second input, so the whole filter chain moves to
   ``-filter_complex``.** Mixing a ``-vf`` with an extra input silently drops the
   overlay, and with ``-filter_complex`` ffmpeg stops auto-selecting streams, so
   the audio has to be mapped back by hand or the clip comes out silent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "media_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("media_cli_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


# --------------------------------------------------------------------------- #
# the bot block
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser",
        "ERROR: [youtube] YLb1TpCKqrA: Unable to extract any player response",
        "WARNING: [youtube] No supported JavaScript runtime could be found.",
        "ERROR: [youtube] nsig extraction failed: Some formats may be unavailable",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    ],
)
def test_real_youtube_refusals_are_recognised(text):
    assert cli._looks_bot_blocked(text) is True


def test_a_plain_network_error_is_not_a_bot_block():
    """Rotating the player client on a dropped connection only makes it slower."""
    assert cli._looks_bot_blocked("ERROR: unable to download webpage: timed out") is False
    assert cli._looks_bot_blocked("") is False


def test_only_youtube_urls_get_the_client_rotation():
    assert cli._is_youtube("https://youtu.be/YLb1TpCKqrA") is True
    assert cli._is_youtube("https://www.youtube.com/watch?v=x") is True
    assert cli._is_youtube("https://vimeo.com/12345") is False


def test_every_rotation_targets_the_youtube_extractor():
    for label, extra in cli._CLIENT_ROTATIONS:
        assert extra[0] == "--extractor-args", label
        assert extra[1].startswith("youtube:player_client="), label


# --------------------------------------------------------------------------- #
# the JavaScript runtime
# --------------------------------------------------------------------------- #
def test_deno_is_preferred_when_several_runtimes_exist(monkeypatch):
    monkeypatch.setattr(cli, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_ytdlp_has_flag", lambda *_a: True)
    assert cli.js_runtime_args("/usr/bin/yt-dlp") == ["--js-runtimes", "deno"]
    assert cli.js_runtime_name() == "deno"


def test_node_is_used_when_deno_is_absent(monkeypatch):
    """Freestyle ships node and not deno, and yt-dlp auto-detects only deno."""
    monkeypatch.setattr(cli, "_which", lambda name: "/usr/local/bin/node" if name == "node" else None)
    monkeypatch.setattr(cli, "_ytdlp_has_flag", lambda *_a: True)
    assert cli.js_runtime_args("/usr/bin/yt-dlp") == ["--js-runtimes", "node"]


def test_no_runtime_means_no_flag_and_no_lie(monkeypatch):
    monkeypatch.setattr(cli, "_which", lambda name: None)
    monkeypatch.setattr(cli, "_ytdlp_has_flag", lambda *_a: True)
    assert cli.js_runtime_args("/usr/bin/yt-dlp") == []
    assert cli.js_runtime_name() is None


def test_an_old_yt_dlp_is_never_handed_a_flag_it_rejects(monkeypatch):
    """A pip fallback in an old image must keep working exactly as before.

    ``--js-runtimes`` is recent; handing it to a build that does not know it makes
    EVERY download fail with a usage error, which is a worse outcome than the
    missing runtime it was meant to fix.
    """
    monkeypatch.setattr(cli, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_ytdlp_has_flag", lambda *_a: False)
    assert cli.js_runtime_args("/usr/bin/yt-dlp") == []


# --------------------------------------------------------------------------- #
# branding
# --------------------------------------------------------------------------- #
def test_no_branding_leaves_the_filter_chain_alone():
    fragment, label, inputs, logo_px = cli.branding_filter(scale_w=1080, scale_h=1920)
    assert (fragment, label, inputs, logo_px) == ("", "base", [], 0)


def test_a_title_is_drawn_through_a_textfile_not_an_inline_string(monkeypatch, tmp_path):
    """The title is exactly the string that breaks inline ``drawtext`` escaping."""
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "_caption_font", lambda: "/fonts/DejaVuSans-Bold.ttf")

    fragment, label, inputs, _ = cli.branding_filter(
        scale_w=1080, scale_h=1920, title="Erasing Foundational Curses: Part 3"
    )

    assert label == "branded"
    assert inputs == []
    assert "drawtext=fontfile=" in fragment
    assert "textfile=" in fragment
    # ``:text=`` is the inline form, and it is the one that needs hand-escaping.
    assert ":text=" not in fragment
    # Apostrophes and colons survive because they never reach the filtergraph.
    written = list((tmp_path / "titles").glob("*.txt"))[0].read_text()
    assert "Erasing Foundational Curses: Part 3" in written.replace("\n", " ")


def test_a_long_title_wraps_and_then_truncates():
    assert cli.wrap_title("Short", max_chars=22) == "Short"
    two = cli.wrap_title("one two three four five six seven eight nine ten eleven twelve",
                         max_chars=22, max_lines=2)
    assert two.count("\n") == 1
    long_word = cli.wrap_title("x" * 200, max_chars=10, max_lines=1)
    assert len(long_word) <= 10


def test_a_logo_becomes_a_second_input_and_an_overlay(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n")

    fragment, label, inputs, logo_px = cli.branding_filter(
        scale_w=1080, scale_h=1920, logo=logo, logo_position="tr"
    )

    assert inputs == ["-i", str(logo)]
    assert label == "withlogo"
    assert logo_px == 172  # 16% of 1080, forced even
    assert "[1:v]scale=172:-2[logo]" in fragment
    assert "overlay=w-overlay_w-" in fragment


def test_the_title_is_centred_clear_of_the_logo(monkeypatch, tmp_path):
    """An overlapping title and logo is unreadable however good the assets are."""
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "_caption_font", lambda: "/fonts/DejaVuSans-Bold.ttf")
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n")

    with_logo, label, _, logo_px = cli.branding_filter(
        scale_w=1080, scale_h=1920, title="Part 3", logo=logo
    )
    without, _, _, _ = cli.branding_filter(scale_w=1080, scale_h=1920, title="Part 3")

    assert label == "branded"
    assert logo_px > 0
    # The title is centred in the width the logo leaves, so the two x expressions
    # differ by exactly the logo's width.
    assert f"(w-{logo_px}-text_w)/2" in with_logo
    assert "(w-0-text_w)/2" in without


def test_a_title_with_no_font_refuses_instead_of_emitting_a_blank_clip(monkeypatch, tmp_path):
    """ffmpeg exits 0 with nothing drawn when the font is missing.

    Silently returning a clip with no title is the failure mode this guards: the
    caller is told the branding was applied and it was not.
    """
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "_caption_font", lambda: None)

    with pytest.raises(cli.MediaError) as excinfo:
        cli.branding_filter(scale_w=1080, scale_h=1920, title="Part 3")

    assert "font" in str(excinfo.value)


def test_filtergraph_paths_are_escaped():
    """``:`` and ``,`` are both legal in a POSIX path and both split a filtergraph."""
    escaped = cli._escape_filter_path("/home/a b/title:1,x.txt")
    assert escaped == "/home/a b/title\\:1\\,x.txt"
