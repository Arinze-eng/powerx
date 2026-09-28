"""Live frames of the sandbox's GUI desktop, pushed to the WebUI.

Why this exists and why it is shaped this way
---------------------------------------------
MetaTrader — and any other GUI app the agent drives — lives inside the execution
sandbox on an Xvfb display. There is **no way to dial into that sandbox**: none
of the backends expose an inbound port (no ``expose_port`` / ``port_forward`` /
``public_url`` anywhere in ``nanobot/agent/tools``), so the conventional
``x11vnc`` + ``noVNC`` setup, which needs a listening TCP port, cannot bind. The
pixels therefore have to be *pulled out* of the sandbox and pushed down the
WebSocket the WebUI already holds.

The alternative that looks easiest — publishing frames through
``nanobot.utils.file_share`` (catbox / onlyfiles) — is deliberately not used. The
sandbox holds live broker credentials, so uploading frames there would publish a
trading terminal, its account number and its open positions, to a public URL.

Numbers this file is built on, measured against a live MT5 terminal
(2026-09-24, 1920x1080):

* 1080p PNG of the terminal: **85 KB**. The same frame as JPEG q60: 272 KB, and
  downscaling it to 720p made the PNG **3.4x larger** — interpolation noise
  defeats deflate where flat UI colours and sharp text compress beautifully. So:
  capture at native geometry and ship PNG. Never resize, never assume JPEG.
* Capture costs ~180 ms (ffmpeg) / ~200 ms (ImageMagick), on a 1 s budget.
* A genuinely static screen captures **byte-identically** (verified with both
  capturers), which is what makes the byte comparison below a valid change
  detector rather than a heuristic: measured **5 of 6 polls suppressed**.

Be clear-eyed about what that dedup does and does not buy, because the obvious
reading is wrong. A *live* terminal is not static — it ticks, changing 1.26 % of
its pixels every 4 s — and byte comparison is all-or-nothing, so a single changed
pixel ships the whole frame: measured **0 of 6 polls suppressed** on the running
terminal, ~88 KB/frame, ~117 KB/frame once base64-wrapped.

So: dedup is a large win for an idle screen and **no win at all** for an active
one. Closing that gap needs tile diffing (send only the changed tiles), which is
deliberately not attempted here. The 1.26 % figure is an argument for tile
diffing, not for this.

Two things this module is deliberately *not*:

* **Not MT5-specific.** The display belongs to the sandbox, not to the trading
  terminal, so any GUI app on that display shows up here.
* **Not driven by the agent loop.** A refresh that only happens during an agent
  turn freezes exactly when a human wants to look at it. The pump is a plain
  background task owned by the gateway process, which is the same process as the
  agent loop (see ``nanobot/cli/gateway_runtime.py``) and therefore already holds
  the sandbox handles.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from loguru import logger

from nanobot.agent.tools.workspace_bridge import (
    RemoteExecutor,
    fetch_remote_file,
    remote_workspace_root,
    resolve_remote_executor,
    run_remote,
)

#: The X display GUI apps inside the sandbox are started on. ``Xvfb :99`` is what
#: ``scripts/install_mt5_sandbox.sh`` brings up, but nothing here depends on MT5:
#: any process drawing to this display is visible.
DEFAULT_DISPLAY = ":99"
DISPLAY_ENV = "POWERX_SCREEN_DISPLAY"

#: Geometry used only by the ffmpeg fallback path. The primary capturer needs no
#: geometry, so a wrong value here cannot crop a frame.
DEFAULT_SIZE = "1920x1080"
SIZE_ENV = "POWERX_SCREEN_SIZE"

#: One capture per this many seconds. Capture alone costs ~0.2 s, so anything
#: below ~0.25 s spends the whole budget inside ImageMagick/ffmpeg.
DEFAULT_INTERVAL_S = 1.0
MIN_INTERVAL_S = 0.25
MAX_INTERVAL_S = 10.0

#: Re-send an unchanged frame at least this often, so a client that subscribes
#: mid-stream, or reconnects after a blip, is never left showing a stale image.
DEFAULT_KEEPALIVE_S = 10.0

#: Frames are written *inside* the sandbox workspace on purpose: every backend's
#: ``download`` is workspace-confined (Runloop and VPS both run a path guard that
#: refuses anything outside it), so a frame in ``/tmp`` could not be fetched.
REMOTE_DIR = ".powerx-screen"
REMOTE_FRAME = "frame.png"

#: A frame is tens of KB. The bound separates "too big" from "transfer failed".
MAX_FRAME_BYTES = 6 * 1024 * 1024

#: The capture script's own refusal text, emitted (exit 3) when the display it was
#: told to capture has no X server or no capturer on it.
#:
#: This is the signal that the sandbox image is *bare* -- a Tenki session is stock
#: Ubuntu with no Xvfb, no ImageMagick and no window manager (measured
#: 2026-09-28: every one of those `MISSING`) -- as opposed to a sandbox whose
#: desktop exists but is unreachable. Only the first case is worth fixing by
#: provisioning, which is what makes this string load-bearing rather than a
#: convenience: provisioning a reachable display's sandbox would be a surprise
#: install, and not provisioning a bare one leaves an empty panel forever.
NO_CAPTURER_MARKER = "no capturer on DISPLAY="

#: Below this many seconds between provisioning attempts, a poll that still finds
#: no capturer is reported as "still starting" instead of re-running the install.
#: ``apt-get update`` plus the install measured 38 s against a live Tenki session,
#: but on a session where ``install_mt5_sandbox.sh`` is running it waits on that
#: installer's own apt (``DPkg::Lock::Timeout``, up to 900 s), so the window has to
#: span a full WineHQ install -- measured 2 m 21 s on 2026-09-28.
PROVISION_RETRY_S = 90.0

#: Give up after this many attempts and say so, rather than installing forever.
#: Sized so the whole window comfortably outlasts an installer that is holding
#: apt: 5 attempts, 90 s apart, is over six minutes of trying.
PROVISION_MAX_ATTEMPTS = 5

#: Shown while the desktop is being installed. Deliberately says what is happening
#: and roughly how long, because the panel is otherwise a blank rectangle for a
#: minute and "no frames yet" reads as a fault.
PROVISIONING_MESSAGE = (
    "This sandbox has no desktop yet, so there is nothing to capture. Installing "
    "Xvfb and the screen-capture tools inside it now; frames start as soon as it "
    "comes up. This takes about a minute, and longer while a MetaTrader install "
    "is still running, because it uses the same package manager."
)

#: Ceiling on the sandbox call that starts the install. The body detaches and the
#: call returns as soon as it has, so this only bounds a hang -- it is not a
#: budget for the install, which is re-checked by later polls instead.
PROVISION_CALL_TIMEOUT_S = 300

PROVISION_FAILED_MESSAGE = (
    "This sandbox could not be given a desktop: the capture tools could not be "
    "installed in it. The panel needs ImageMagick (import) or ffmpeg and a running "
    "X display; see desktop.log in the sandbox for what the install said."
)

#: Only an X display spec may be interpolated into the provisioning shell. The
#: capture script relies on the display having been validated upstream
#: (``runtime._clean_display``); this repeats the check because the value here
#: reaches a shell that runs as the sandbox user, with ``sudo`` available.
_DISPLAY_SPEC_RE = re.compile(r"^:[0-9]{1,3}(\.[0-9]{1,2})?$")

#: Xvfb geometry is taken from the same ``WxH`` spec the ffmpeg fallback uses, so
#: the two paths capture the same rectangle instead of disagreeing about it.
_SIZE_RE = re.compile(r"^[0-9]{3,4}x[0-9]{3,4}$")

#: The one apt lock every writer of apt on a sandbox takes before it runs.
#:
#: MEASURED 2026-09-28, live Tenki session, MT5 + Live screen together: the two
#: apt runs on one fresh sandbox destroy each other. ``install_mt5_sandbox.sh``
#: had started its own ``apt-get install xvfb winbind ...`` and the pump, finding
#: no capturer, started a second one -- and the installer's dpkg then failed with
#:
#:   dpkg: error processing archive /tmp/apt-dpkg-install-XXXXXX/81-xvfb_....deb
#:         (--unpack): cannot access archive ...: No such file or directory
#:
#: for the last sixteen packages of its transaction, so **xvfb was never
#: installed**, the installer swallowed the failure with ``|| true`` and carried
#: on with no display, and the pump's own install answered ``E: Could not get
#: lock /var/lib/dpkg/lock-frontend``. Net effect: an empty Live screen forever,
#: which is exactly the bug this provisioning exists to fix.
#:
#: This is why Novita never showed it: that image ships Xvfb and ImageMagick, so
#: the pump never provisions there and there is only ever one apt run.
APT_LOCK_PATH = "/tmp/powerx-apt.lock"

#: apt's own patience, passed to every apt call the body makes. If both writers
#: take the lock *and* pass these, a second run waits for the first instead of
#: failing on it -- belt and braces, because the lock file only helps while both
#: sides are updated copies of this repo.
APT_LOCK_TIMEOUT_S = 900

#: How long the provisioning body waits for the *MetaTrader installer's* apt
#: before giving up and letting the next poll try again.
#:
#: Bounded, not generous, and that is a measured correction: a first cut of this
#: waited the full :data:`APT_LOCK_TIMEOUT_S`. The installer took the lock and
#: then KEPT it, because it held the file descriptor for the life of the script
#: and every long-lived process it started -- Xvfb, ``matchbox-window-manager``,
#: ``wineserver``, ``terminal64.exe`` -- inherited that descriptor and went on
#: holding the lock after the installer exited. Measured live on Tenki: a
#: ``flock -w 900 9`` in the body sat waiting 4 minutes and counting against a
#: holder that was nothing but a running Xvfb, so the panel showed no frame at
#: all while ``import`` was missing. Both sides now take the lock per apt
#: transaction with ``flock --close``, which no child can inherit, and the body
#: never waits longer than this.
APT_LOCK_WAIT_S = 120

#: The provisioning body. Detached with its own log, and written without a single
#: apostrophe so it can be wrapped in ``sh -c '...'``: the display and geometry
#: arrive through ``PX_DISPLAY`` / ``PX_SIZE`` and every path through a variable,
#: so nothing inside needs quoting.
#:
#: Five things here are load-bearing, and each was measured wrong on a live run
#: first:
#:
#: * every background start is wrapped in ``if``. ``pgrep ... || cmd &`` does not
#:   mean "start cmd in the background if it is missing" -- ``&`` binds the whole
#:   and-or list, so the *check* was backgrounded and the script raced past the
#:   start it was supposed to wait for. The window manager never came up that way
#:   while Xvfb did, purely because a ``sleep`` happened to follow one of them;
#:
#: * the window manager is matched with ``pgrep -x matchbox-window``, never with
#:   ``pgrep -f matchbox-window-manager``. ``-f`` matches the full command line,
#:   and the command line of the shell *running this body* contains that literal
#:   string -- so the check matched its own shell, decided a window manager was
#:   already up, and never started one. ``-x`` matches the process name instead,
#:   which cannot contain this script. It is ``matchbox-window`` and not the full
#:   name because the kernel truncates the process name to 15 characters, so the
#:   longer pattern would match nothing at all;
#: * ``Xvfb`` is matched as ``"Xvfb $D"`` -- after expansion, e.g. ``Xvfb :99`` --
#:   which the body does not contain literally (the body writes ``Xvfb "$D"``), so
#:   that check is genuinely about the running server;
#: * apt is serialised, and only for as long as an apt transaction lasts. Each
#:   apt call is run through ``flock --close`` on :data:`APT_LOCK_PATH`, which
#:   holds the lock while the command runs and hands no descriptor to it or to
#:   anything it starts -- see :data:`APT_LOCK_WAIT_S` for the leak that measured
#:   cost. The lock is probed first and never queued on for longer than that: a
#:   holder that will never release it (a process left by an older revision, say)
#:   must not be able to stall the panel, so the body falls back to running apt
#:   under apt's own lock timeouts and says so in the log. Waiting is bounded, and only the first attempt refreshes the apt
#:   indexes: a later attempt is a poll that has just queued behind the
#:   MetaTrader installer's apt, which needs an install and not another refresh.
#:   The tools are re-checked at the top of every attempt, so an install the
#:   MetaTrader installer completed while we waited ends the loop;

#: * the install is skipped entirely when the tools are already there, so a
#:   sandbox whose desktop merely had not been started yet is never given an apt
#:   run for nothing.
_PROVISION_BODY = (
    'set -u; D="$PX_DISPLAY"; '
    'LOCK="' + APT_LOCK_PATH + '"; WAIT=' + str(APT_LOCK_WAIT_S) + '; '
    'OPTS="-o APT::Lock::Timeout=' + str(APT_LOCK_TIMEOUT_S) + ' -o DPkg::Lock::Timeout=' + str(APT_LOCK_TIMEOUT_S) + '"; '
    'PKGS="xvfb imagemagick x11-apps matchbox-window-manager xterm"; '
    'n=0; '
    'while [ "$n" -lt 4 ]; do n=$((n + 1)); '
    'need=0; command -v Xvfb >/dev/null 2>&1 || need=1; '
    'command -v import >/dev/null 2>&1 || need=1; '
    '[ "$need" = 0 ] && break; '
    'if command -v apt-get >/dev/null 2>&1; then '
    'if [ "$(id -u)" = 0 ]; then APT="apt-get"; '
    "elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then "
    'APT="sudo -n env DEBIAN_FRONTEND=noninteractive apt-get"; else APT=""; fi; '
    'if [ -n "$APT" ]; then echo "desktop install attempt $n"; '
    'LK=""; if command -v flock >/dev/null 2>&1; then '
    'if flock -w $WAIT --close $LOCK true; then LK="flock -w $WAIT --close $LOCK"; '
    'else echo shared-apt-lock-busy-using-apt-timeouts; fi; fi; '
    '[ "$n" = 1 ] && $LK $APT $OPTS update -qq; '
    "$LK $APT $OPTS install -y -qq --no-install-recommends $PKGS; "
    "command -v import >/dev/null 2>&1 || "
    "$LK $APT $OPTS --fix-missing install -y -qq --no-install-recommends imagemagick; "
    "fi; fi; sleep 10; done; "
    'command -v Xvfb >/dev/null 2>&1 || { echo no-Xvfb-available; exit 4; }; '
    'if ! pgrep -f "Xvfb $D" >/dev/null 2>&1; then '
    'nohup Xvfb "$D" -screen 0 "$PX_SIZE"x24 >>"$PX_LOG" 2>&1 & fi; '
    "sleep 2; "
    'pgrep -f "Xvfb $D" >/dev/null 2>&1 || { echo Xvfb-did-not-start; exit 5; }; '
    "if ! pgrep -x matchbox-window >/dev/null 2>&1; then "
    'DISPLAY="$D" nohup matchbox-window-manager -use_titlebar no >>"$PX_LOG" 2>&1 & fi; '
    "if command -v xterm >/dev/null 2>&1; then "
    "if ! pgrep -x xterm >/dev/null 2>&1; then "
    'DISPLAY="$D" nohup xterm -fa Monospace -fs 11 -geometry 100x28+0+0 '
    '>>"$PX_LOG" 2>&1 & fi; fi; '
    "sleep 1; mkdir -p \"$PX_DIR\"; "
    'printf "wm="; pgrep -x matchbox-window | head -1; '
    'printf "xvfb="; pgrep -f "Xvfb $D" | head -1; echo; '
    'DISPLAY="$D" import -window root "$PX_DIR/frame.png" >/dev/null 2>&1 && '
    "echo desktop-ready || echo desktop-up"
)


def desktop_provision_command(
    display: str, out_dir: str, size: str = DEFAULT_SIZE
) -> str:
    """Shell that gives a bare sandbox a capturable desktop, detached.

    Detached on purpose: the install measured **38 s** inside a live Tenki
    session, and holding the capture pump's command open that long would freeze
    the panel it is trying to fill. The pump polls instead -- the same contract
    ``install_mt5_sandbox.sh`` and ``install_engineering_draw.sh`` use, which is
    also why this installs the display stack those scripts install (Xvfb, a window
    manager with a window on it, and ImageMagick's ``import``).

    ``setsid`` where it exists, because on Tenki the sandbox's own ``shell`` call
    does not return until the process group does: the detached install measured
    **72 s** of wall clock on the caller even though it backgrounds its work. That
    is an own session with no controlling terminal (``< /dev/null`` as well), so
    the installer cannot be reaped by the caller's timeout -- and the caller's
    timeout is raised in :meth:`SandboxScreenSource._provision_desktop` to match
    the longest the lock wait can legally take.

    A window manager and an ``xterm`` are started as well as Xvfb: a bare Xvfb
    serves a black root window, and a black rectangle is indistinguishable from a
    broken panel. The ``xterm`` gives the operator something to look at, and any
    GUI the agent starts afterwards appears on the same display.

    ``display`` and ``size`` are re-validated rather than trusted: both reach a
    shell, and the sandbox user has ``sudo``.
    """
    spec = display.strip() if isinstance(display, str) else ""
    if not _DISPLAY_SPEC_RE.match(spec):
        spec = DEFAULT_DISPLAY
    geometry = size.strip() if isinstance(size, str) else ""
    if not _SIZE_RE.match(geometry):
        geometry = DEFAULT_SIZE
    quoted_dir = shlex.quote(out_dir)
    quoted_log = shlex.quote(f"{out_dir.rstrip('/')}/desktop.log")
    # The display and geometry are spliced unquoted into an ``env`` argument list.
    # Both are now known to be a bare ``:99`` / ``1920x1080`` and cannot carry a
    # metacharacter, which is what makes that safe; a path cannot make that claim
    # and so goes through ``shlex.quote``.
    launch = (
        f"env PX_DISPLAY={spec} PX_SIZE={geometry} "
        f"PX_LOG={quoted_log} PX_DIR={quoted_dir} "
        f"sh -c {shlex.quote(_PROVISION_BODY)}"
    )
    return (
        f"mkdir -p {quoted_dir} && "
        "if command -v setsid >/dev/null 2>&1; then "
        f"setsid nohup {launch} >> {quoted_log} 2>&1 < /dev/null & "
        f"else nohup {launch} >> {quoted_log} 2>&1 < /dev/null & fi; "
        "echo provisioning-started"
    )

#: How long to wait before retrying after a failed capture, so a sandbox that is
#: down does not turn into a hot loop hammering it.
ERROR_BACKOFF_S = 3.0

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: What the gateway calls to hand one frame to one subscriber.
FrameSink = Callable[..., Awaitable[None]]

#: What the gateway calls with a human-readable reason when a stream has nothing to
#: show. A cleared error (``None`` -- frames are arriving again) is not sent,
#: because the frame itself carries the recovery.
ErrorSink = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ScreenFrame:
    """One captured desktop image."""

    seq: int
    data: bytes
    content_type: str
    captured_at: float
    width: int | None
    height: int | None
    changed: bool
    display: str

    def payload(self, *, replay: bool = False) -> dict[str, object]:
        """Serialise for the wire.

        The image is base64 in a JSON envelope rather than a binary WebSocket
        frame. Base64 costs 33 % on an 85 KB frame — about 28 KB — and buys the
        client no new protocol to implement: it already parses JSON envelopes and
        dispatches on ``event``. A duplex binary channel is what tier 3 (real VNC
        input) needs, not this.
        """
        body: dict[str, object] = {
            "seq": self.seq,
            "content_type": self.content_type,
            "width": self.width,
            "height": self.height,
            "changed": self.changed,
            "display": self.display,
            "captured_at": self.captured_at,
            "bytes": len(self.data),
            "image": base64.b64encode(self.data).decode("ascii"),
        }
        if replay:
            body["replay"] = True
        return body


def image_size(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from a PNG's IHDR chunk, or ``None``.

    Read from the bytes rather than trusting a probed geometry: the header is
    authoritative about what was actually captured, so a mismatch shows up as a
    smaller frame instead of silently cropping.
    """
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    if data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class ScreenSource(Protocol):
    """Where frames come from. Implemented for the sandbox, and for the host."""

    display: str

    #: ``"sandbox"`` or ``"host"``. Reported to the panel so an operator can
    #: tell which machine they are looking at instead of assuming.
    location: str

    async def resolve(self) -> None:
        """Settle anything that decides :attr:`location`, before it is reported.

        A source that picks its delegate lazily would otherwise have its
        location read before the choice was made, and the panel would be told
        the wrong machine. Implementations that know already do nothing.
        """
        ...

    async def capture(self) -> bytes | None: ...

    async def diagnostic(self) -> str: ...


# ---------------------------------------------------------------------------
# Sandbox source
# ---------------------------------------------------------------------------


def host_display_is_reachable(display: str) -> bool:
    """True when *display* looks like it is served by **this** machine.

    Checked against the X11 unix socket directory rather than by running
    ``xdpyinfo``: it is a filesystem stat instead of a subprocess, and it cannot
    be fooled by a stale ``DISPLAY`` pointing at another host. Only local
    ``:N`` displays are recognised — a ``host:0`` TCP display is somebody else's
    machine and must not be mistaken for the gateway's own desktop.
    """
    spec = display.strip()
    if not spec.startswith(":"):
        return False
    number = spec[1:].split(".", 1)[0]
    if not number.isdigit():
        return False
    return os.path.exists(f"/tmp/.X11-unix/X{number}")


class SandboxScreenSource:
    """Capture the sandbox's X display and pull the frame out as PNG bytes."""

    def __init__(
        self,
        *,
        display: str | None = None,
        size: str | None = None,
        executor: RemoteExecutor | None = None,
        session_key: str | None = None,
    ) -> None:
        self.display = (
            display or os.environ.get(DISPLAY_ENV) or DEFAULT_DISPLAY
        ).strip() or DEFAULT_DISPLAY
        self.size = (size or os.environ.get(SIZE_ENV) or DEFAULT_SIZE).strip() or DEFAULT_SIZE
        self._executor = executor
        # Which sandbox to attach to. The pump has no request context, so it must
        # be told — see :func:`resolve_remote_executor`.
        self.session_key = session_key
        self.location = "sandbox"
        self._frame_path: str | None = None
        self.last_error: str | None = None
        # Provisioning bookkeeping. ``_provision_attempts`` is capped so a sandbox
        # that cannot take the install is reported honestly once instead of being
        # retried forever, and ``_provision_at`` spaces attempts out.
        self._provision_attempts = 0
        self._provision_at: float | None = None

    async def _resolve(self) -> RemoteExecutor:
        """Resolve (and cache) the sandbox handle, re-resolving when it drops."""
        if self._executor is None or not self._executor.available:
            self._executor = await resolve_remote_executor(session_key=self.session_key)
        return self._executor

    async def _path(self, executor: RemoteExecutor) -> str | None:
        """Absolute in-sandbox path for the frame, cached per source."""
        if self._frame_path is not None:
            return self._frame_path
        if executor.backend is not None:
            root = (
                await remote_workspace_root(session_key=self.session_key)
            ) or "/workspace"
        else:
            from nanobot.agent.tools.novita_sandbox import _WORKSPACE  # noqa: PLC2701

            root = _WORKSPACE
        self._frame_path = f"{root.rstrip('/')}/{REMOTE_DIR}/{REMOTE_FRAME}"
        return self._frame_path

    def _script(self, out: str) -> str:
        """Shell that captures the display and prints the frame's byte count.

        ImageMagick is tried first even though ffmpeg is marginally faster
        (~180 ms vs ~200 ms, on a 1 s budget). ``import -window root`` needs no
        geometry argument, so it cannot crop or letterbox; ``ffmpeg -f x11grab``
        needs an exact ``-video_size`` and silently produces a wrong frame when
        the display does not match the guess. Robustness is worth 20 ms here.
        """
        display = shlex.quote(self.display)
        quoted_out = shlex.quote(out)
        ffmpeg = (
            f"DISPLAY={display} ffmpeg -hide_banner -loglevel error "
            f"-f x11grab -video_size {shlex.quote(self.size)} -i {display} "
            f"-frames:v 1 -y {quoted_out} >/dev/null 2>&1"
        )
        return (
            "set -u; "
            f'mkdir -p "$(dirname {quoted_out})"; '
            f"rm -f {quoted_out}; "
            f"if command -v import >/dev/null 2>&1; then "
            f"DISPLAY={display} import -window root {quoted_out} >/dev/null 2>&1; "
            f"fi; "
            f"if [ ! -s {quoted_out} ] && command -v ffmpeg >/dev/null 2>&1; then "
            f"{ffmpeg}; "
            f"fi; "
            f"if [ ! -s {quoted_out} ]; then "
            f"echo 'no capturer on DISPLAY={self.display}: need ImageMagick import "
            f"or ffmpeg, and a running X display' >&2; exit 3; "
            f"fi; "
            f"stat -c %s {quoted_out}"
        )

    async def resolve(self) -> None:
        """Nothing to decide: this source always captures in the sandbox."""
        return None

    async def _provision_desktop(self, executor: RemoteExecutor, out: str) -> None:
        """Give a bare sandbox an X desktop, and report it while that happens.

        Only called when the capture script refused for *want of a capturer*
        (:data:`NO_CAPTURER_MARKER`) -- see that constant for why the distinction
        between "no desktop" and "desktop unreachable" matters.

        The install is started detached and returns immediately, so the pump keeps
        polling and picks up the frame on the first capture after Xvfb is up. Until
        then ``last_error`` says what is going on, which the panel shows: without
        that the operator sees an empty rectangle with no explanation for a minute.
        """
        now = time.monotonic()
        if self._provision_attempts >= PROVISION_MAX_ATTEMPTS:
            self.last_error = PROVISION_FAILED_MESSAGE
            return
        if self._provision_at is not None and now - self._provision_at < PROVISION_RETRY_S:
            # An install is already in flight. Re-running apt now would only
            # contend with it, so report progress and let the next poll decide.
            self.last_error = PROVISIONING_MESSAGE
            return
        self._provision_attempts += 1
        self._provision_at = now
        self.last_error = PROVISIONING_MESSAGE
        out_dir = out.rsplit("/", 1)[0] if "/" in out else out
        # The caller must outlast the body own lock wait, or the timeout would
        # land on an install that is simply queueing behind the MT5 installer.
        # The body returns as soon as it has detached, so this is a ceiling on a
        # hang, not on the install.
        ok, output = await run_remote(
            desktop_provision_command(self.display, out_dir, self.size),
            timeout=PROVISION_CALL_TIMEOUT_S,
            executor=executor,
        )
        if not ok:
            logger.debug(
                "screen_stream: could not start desktop provisioning on {}: {}",
                self.display,
                (output or "").strip()[-200:],
            )

    async def capture(self) -> bytes | None:
        """Capture one frame, or ``None`` when the sandbox cannot produce it."""
        executor = await self._resolve()
        if not executor.available:
            self.last_error = "no execution sandbox is configured"
            return None
        out = await self._path(executor)
        if out is None:
            self.last_error = "could not resolve the sandbox workspace"
            return None
        ok, output = await run_remote(self._script(out), timeout=60, executor=executor)
        if not ok:
            text = (output or "").strip()
            if NO_CAPTURER_MARKER in text:
                await self._provision_desktop(executor, out)
                return None
            self.last_error = text[-300:] or "capture command failed"
            return None
        data = await fetch_remote_file(out, max_bytes=MAX_FRAME_BYTES, executor=executor)
        if not data:
            self.last_error = "capture produced no readable frame"
            return None
        self.last_error = None
        return data

    async def diagnostic(self) -> str:
        """Explain why no frames are arriving, for the panel to show."""
        executor = await self._resolve()
        if not executor.available:
            return "No execution sandbox is configured, so there is no desktop to show."
        return self.last_error or "No frames yet."


# ---------------------------------------------------------------------------
# Host source (tests, and a gateway running on the GUI box itself)
# ---------------------------------------------------------------------------


class LocalScreenSource:
    """Capture a display on the **host** by running the same script locally.

    Used by tests, and legitimately useful when the gateway runs on the same
    machine as the GUI (a VPS the operator owns), where the sandbox round trip is
    pure overhead.
    """

    def __init__(self, *, display: str | None = None, size: str | None = None) -> None:
        self.display = (
            display or os.environ.get(DISPLAY_ENV) or DEFAULT_DISPLAY
        ).strip() or DEFAULT_DISPLAY
        self.size = (size or os.environ.get(SIZE_ENV) or DEFAULT_SIZE).strip() or DEFAULT_SIZE
        self.location = "host"
        self.last_error: str | None = None
        self._delegate = SandboxScreenSource(display=self.display, size=self.size)

    async def resolve(self) -> None:
        """Nothing to decide: this source always captures on the host."""
        return None

    async def capture(self) -> bytes | None:
        out = f"{os.path.sep}tmp{os.path.sep}powerx-screen-local.png"
        proc = await asyncio.create_subprocess_shell(
            self._delegate._script(out),  # noqa: SLF001 - same package, same script
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            self.last_error = "capture timed out"
            return None
        if proc.returncode != 0:
            self.last_error = (stderr or b"").decode("utf-8", "replace").strip()[-300:]
            return None
        try:
            with open(out, "rb") as handle:
                data = handle.read(MAX_FRAME_BYTES + 1)
        except OSError as exc:
            self.last_error = f"could not read frame: {exc}"
            return None
        if not data or len(data) > MAX_FRAME_BYTES:
            self.last_error = "capture produced no readable frame"
            return None
        self.last_error = None
        return data

    async def diagnostic(self) -> str:
        return self.last_error or "No frames yet."


class AutoScreenSource:
    """Prefer the sandbox; fall back to the host when there is no sandbox to use.

    Two deployments have to work, and they disagree about where the display is:

    * **A hosted sandbox** (Novita, Runloop, Daytona, ...). The gateway runs
      somewhere else entirely, the sandbox holds the desktop, and the frame has
      to be captured remotely and pulled out. The host has no such display, so
      :func:`host_display_is_reachable` is false and this always takes this path.
    * **The GUI box itself** (a VPS the operator owns, or the machine this repo's
      installer targets). No execution backend is provisioned at all, the desktop
      is local, and routing a capture through a sandbox handle that does not
      exist produced nothing but an error.

    The fallback is therefore gated on both halves of the question — *is there a
    usable sandbox* **and** *is this display actually reachable here* — rather
    than on either alone:

    * No sandbox handle and a reachable local display: the desktop is local.
    * No sandbox handle and no local display: nothing to show. Staying on the
      sandbox source makes ``diagnostic()`` say so instead of quietly capturing
      an unrelated machine.
    * A sandbox handle that is merely *unreachable*: no fallback. The frame would
      otherwise show the gateway host's desktop while the operator believed they
      were looking at the sandbox, which is worse than an error.

    Whichever wins is reported as :attr:`location`, so the panel says where the
    pixels came from instead of the operator having to guess.
    """

    def __init__(
        self,
        *,
        display: str | None = None,
        size: str | None = None,
        session_key: str | None = None,
    ) -> None:
        self.display = (
            display or os.environ.get(DISPLAY_ENV) or DEFAULT_DISPLAY
        ).strip() or DEFAULT_DISPLAY
        self.session_key = session_key
        self._size = size
        self._delegate: SandboxScreenSource | LocalScreenSource = SandboxScreenSource(
            display=display, size=size, session_key=session_key
        )
        self._decided = False
        self.last_error: str | None = None

    @property
    def location(self) -> str:
        return self._delegate.location

    async def _pick(self) -> None:
        """Choose a delegate once, on the first capture."""
        executor = await self._delegate._resolve()  # noqa: SLF001 - same package
        if not executor.available and host_display_is_reachable(self.display):
            logger.debug(
                "screen_stream: no {} sandbox handle for {}, capturing {} on the host",
                executor.name,
                self.session_key,
                self.display,
            )
            self._delegate = LocalScreenSource(display=self.display, size=self._size)
        self._decided = True

    async def resolve(self) -> None:
        """Settle the delegate now, so :attr:`location` is truthful when read."""
        if not self._decided:
            await self._pick()

    async def capture(self) -> bytes | None:
        await self.resolve()
        data = await self._delegate.capture()
        self.last_error = self._delegate.last_error
        return data

    async def diagnostic(self) -> str:
        await self.resolve()
        return await self._delegate.diagnostic()


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class ScreenStream:
    """One session's capture pump and subscriber fan-out.

    The pump runs while at least one subscriber is attached and stops when the
    last one leaves, so closing the panel costs nothing.
    """

    def __init__(
        self,
        key: str,
        source: ScreenSource,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        keepalive_s: float = DEFAULT_KEEPALIVE_S,
    ) -> None:
        self.key = key
        self.source = source
        self.interval_s = min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, float(interval_s)))
        self.keepalive_s = max(self.interval_s, float(keepalive_s))
        self._subscribers: set[FrameSink] = set()
        self._error_sinks: set[ErrorSink] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._last_pushed: ScreenFrame | None = None
        self._last_push_at = 0.0
        self._seq = 0
        self.error: str | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_frame(self) -> ScreenFrame | None:
        return self._last_pushed

    async def subscribe(self, sink: FrameSink) -> None:
        """Attach *sink*; it immediately receives the newest frame if there is one."""
        async with self._lock:
            self._subscribers.add(sink)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._pump(), name=f"screen-pump:{self.key}")
        if self._last_pushed is not None:
            # A new subscriber must not stare at an empty panel until the screen
            # next changes — an idle terminal can be static for minutes.
            await self._emit(sink, self._last_pushed, replay=True)

    async def add_error_sink(self, sink: ErrorSink) -> None:
        """Attach a sink for "why nothing is arriving" messages."""
        self._error_sinks.add(sink)
        if self.error is not None:
            # A panel opened while the stream is already failing should say so now
            # rather than after the next failed poll.
            with contextlib.suppress(Exception):
                await sink(self.error)

    async def remove_error_sink(self, sink: ErrorSink) -> None:
        self._error_sinks.discard(sink)

    async def _publish_error(self, text: str | None) -> None:
        """Tell error sinks why nothing is arriving, when that changes.

        The pump's failure path emits no frame, so the reason has to travel on its
        own event: without it the panel is an empty rectangle, which is
        indistinguishable from a broken panel. Repeated identical diagnostics are
        dropped, because the pump re-diagnoses on every backoff and the operator
        does not need the same sentence once a second.
        """
        if text == self.error:
            return
        self.error = text
        if text is None:
            # Recovery is carried by the frame that resumes the stream.
            return
        for sink in tuple(self._error_sinks):
            try:
                await sink(text)
            except Exception as exc:  # noqa: BLE001 - a dead subscriber is expected
                logger.debug("screen_stream: dropping error sink for {}: {}", self.key, exc)
                self._error_sinks.discard(sink)

    async def unsubscribe(self, sink: FrameSink) -> None:
        async with self._lock:
            self._subscribers.discard(sink)
            if not self._subscribers:
                await self._stop_locked()

    async def stop(self) -> None:
        async with self._lock:
            self._subscribers.clear()
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            # Await the cancelled task so the pump cannot outlive the stream and
            # keep writing frames nobody is listening for.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _emit(self, sink: FrameSink, frame: ScreenFrame, *, replay: bool) -> None:
        try:
            await sink(frame, replay=replay)
        except Exception as exc:  # noqa: BLE001 - a dead subscriber is expected
            logger.debug("screen_stream: dropping subscriber for {}: {}", self.key, exc)
            self._subscribers.discard(sink)

    async def _fanout(self, frame: ScreenFrame) -> None:
        for sink in tuple(self._subscribers):
            await self._emit(sink, frame, replay=False)

    async def _pump(self) -> None:
        """Capture on a timer, and push only when the screen actually moved."""
        try:
            while True:
                started = time.monotonic()
                data = await self.source.capture()
                if data is None:
                    await self._publish_error(await self.source.diagnostic())
                    await asyncio.sleep(ERROR_BACKOFF_S)
                    continue

                now = time.monotonic()
                # Byte equality against the last frame we *pushed* (not the last
                # we captured) is the change detector. Verified: an unchanged
                # screen produces identical bytes from both capturers, and 5 of 6
                # polls are suppressed when nothing moves.
                #
                # Note the limit: this is all-or-nothing. A ticking terminal ships
                # every frame (~88 KB each) no matter how few pixels moved — see
                # the module docstring. Tile diffing is the fix, and is not done.
                changed = self._last_pushed is None or data != self._last_pushed.data
                keepalive_due = (now - self._last_push_at) >= self.keepalive_s
                if changed or keepalive_due:
                    self._seq += 1
                    size = image_size(data)
                    frame = ScreenFrame(
                        seq=self._seq,
                        data=data,
                        content_type="image/png",
                        captured_at=now,
                        width=size[0] if size else None,
                        height=size[1] if size else None,
                        changed=changed,
                        display=self.source.display,
                    )
                    self._last_pushed = frame
                    self._last_push_at = now
                    # Clears ``error`` for readers of the stream's own state; the
                    # sinks are not told, because the frame is the notification.
                    self.error = None
                    await self._fanout(frame)

                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, self.interval_s - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the pump must outlive surprises
            logger.warning("screen_stream: pump for {} stopped: {}", self.key, exc)
            self.error = str(exc)[:300]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ScreenStreamManager:
    """Owns the per-session streams for a gateway process."""

    def __init__(
        self,
        source_factory: Callable[[str | None, str | None], ScreenSource] | None = None,
        *,
        default_interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """Create a manager.

        ``source_factory`` maps ``(display, session_key)`` to the capture source.
        It exists so tests can supply a scripted source, without this class having
        to know how a display is reached.
        """
        self._streams: dict[str, ScreenStream] = {}
        self._lock = asyncio.Lock()
        self._source_factory = source_factory or (
            lambda display, session_key: AutoScreenSource(
                display=display, session_key=session_key
            )
        )
        self._default_interval_s = default_interval_s

    def stream(self, key: str) -> ScreenStream | None:
        return self._streams.get(key)

    async def subscribe(
        self,
        key: str,
        sink: FrameSink,
        *,
        display: str | None = None,
        interval_s: float | None = None,
        session_key: str | None = None,
        error_sink: ErrorSink | None = None,
    ) -> ScreenStream:
        """Attach *sink* to the session's stream, creating the stream if needed.

        *session_key* is the sandbox this stream captures, and is only consulted
        when the stream is created: one stream per key means every later
        subscriber joins the sandbox the first one named.

        *error_sink* is attached as well when given, so the subscriber learns why
        the stream is empty instead of waiting on an image that is not coming.
        """
        async with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                source = self._source_factory(display, session_key)
                stream = ScreenStream(
                    key,
                    source,
                    interval_s=interval_s or self._default_interval_s,
                )
                # Settle the source's choice of machine before anyone can read
                # ``location`` off it, so the subscribe ack cannot name the wrong
                # one. Not under the lock beyond construction: the resolver only
                # builds a client object, it does not wait on the sandbox.
                with contextlib.suppress(Exception):
                    await source.resolve()
                self._streams[key] = stream
        await stream.subscribe(sink)
        if error_sink is not None:
            await stream.add_error_sink(error_sink)
        return stream

    async def add_error_sink(self, key: str, sink: ErrorSink) -> None:
        """Register an error sink on an existing stream, if it still exists."""
        stream = self._streams.get(key)
        if stream is not None:
            await stream.add_error_sink(sink)

    async def remove_error_sink(self, key: str, sink: ErrorSink) -> None:
        """Detach an error sink; a stream that has already gone is not an error."""
        stream = self._streams.get(key)
        if stream is not None:
            await stream.remove_error_sink(sink)

    async def unsubscribe(self, key: str, sink: FrameSink) -> None:
        async with self._lock:
            stream = self._streams.get(key)
        if stream is None:
            return
        await stream.unsubscribe(sink)
        if stream.subscriber_count == 0:
            async with self._lock:
                # Re-check under the lock: a concurrent subscribe may have
                # re-attached between the check and here.
                if stream.subscriber_count == 0 and self._streams.get(key) is stream:
                    self._streams.pop(key, None)
            if stream.subscriber_count == 0:
                await stream.stop()

    async def release_sink(self, sink: FrameSink) -> None:
        """Detach one sink from every stream — used when a client disconnects."""
        for key in list(self._streams):
            await self.unsubscribe(key, sink)

    async def shutdown(self) -> None:
        async with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            await stream.stop()

    @property
    def active_keys(self) -> list[str]:
        return sorted(self._streams)
