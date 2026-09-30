#!/usr/bin/env python3
"""Headless Android control, from the command line, inside an execution sandbox.

WHY THIS EXISTS
---------------
The agent needs to run an Android app the way it already runs MetaTrader under
Wine: entirely from the shell, with no GUI and no human touching a screen. A
windowless, KVM-accelerated Android emulator plus ``adb`` is exactly that: adb
is a complete programmatic surface -- install an APK, launch it, inject taps and
text, read the live view hierarchy, take screenshots, push and pull files, read
logcat. Nothing here needs a display server.

WHY NOT WAYDROID
----------------
Waydroid is lighter and was the first choice. It is impossible here, and this is
a MEASURED fact rather than a preference: Waydroid's container requires the
``binder_linux`` kernel module, and the sandbox runs a provider kernel
(6.1.102) that ships neither ``binder_linux`` nor ``ashmem_linux`` and has no
``/lib/modules/<ver>/build`` headers -- so the module can be neither loaded nor
compiled. The emulator carries its own kernel and needs no host module. The
sandbox does expose ``/dev/kvm`` and nested virtualisation works, so the
emulator gets hardware acceleration. Both facts were verified live.

CONTRACT
--------
Every action prints exactly ONE JSON object on stdout. The tool layer parses the
last balanced ``{...}`` block, so a stray library warning can never be mistaken
for the answer. Failures set ``ok: false`` and carry a ``next`` hint.

``--help`` on any action prints its flags; ``doctor`` prints what is installed.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

#: MUST be kept equal to ``_CLI_VERSION`` in nanobot/agent/tools/android_sandbox.py.
#: The tool's bootstrap refuses to run a CLI that does not carry this marker, so a
#: stale cached copy is detected loudly instead of being debugged as if it were
#: the new one. Bump BOTH whenever the contract changes.
CLI_VERSION = "2026-09-30.1"

HOME_DIR = Path(os.environ.get("ANDROID_HOME_DIR", str(Path.home() / ".android_box")))
SDK = Path(os.environ.get("ANDROID_SDK_ROOT", str(Path.home() / "android-sdk")))
AVD_NAME = os.environ.get("ANDROID_AVD_NAME", "powerx")
SYSTEM_IMAGE = os.environ.get("ANDROID_SYSTEM_IMAGE", "system-images;android-30;google_apis;x86_64")
INSTALL_LOG = HOME_DIR / "install.log"
INSTALL_DONE = HOME_DIR / ".install.done"
BOOT_LOG = HOME_DIR / "emulator.log"

ADB = str(SDK / "platform-tools" / "adb")
EMULATOR = str(SDK / "emulator" / "emulator")
AVD_HOME = Path(os.environ.get("ANDROID_AVD_HOME", str(Path.home() / ".android" / "avd")))

#: The emulator's console port. Fixed so `adb` and `state` address one device.
EMULATOR_PORT = int(os.environ.get("ANDROID_EMULATOR_PORT", "5554"))
SERIAL = f"emulator-{EMULATOR_PORT}"

#: Boot is the one genuinely slow step (a cold Android userspace). Measured on a
#: Freestyle VM: ~40 s to ``sys.boot_completed``. The ceiling is generous because
#: a cold snapshot restore on a busy host is slower, not because it should take
#: this long.
BOOT_TIMEOUT_S = int(os.environ.get("ANDROID_BOOT_TIMEOUT_S", "420"))


# --------------------------------------------------------------------------- #
# process plumbing
# --------------------------------------------------------------------------- #
def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["ANDROID_SDK_ROOT"] = str(SDK)
    env["ANDROID_HOME"] = str(SDK)
    env["ANDROID_AVD_HOME"] = str(AVD_HOME)
    env["PATH"] = f"{SDK / 'platform-tools'}:{SDK / 'emulator'}:{env.get('PATH', '')}"
    # adb otherwise tries to use the caller's console for interactive prompts.
    env.setdefault("ADB_TRACE", "")
    return env


def run(cmd: list[str] | str, timeout: int = 300, check: bool = False) -> dict[str, Any]:
    """Run a command, never raise, and report what happened."""
    shell = isinstance(cmd, str)
    try:
        proc = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(),
        )
        return {
            "code": proc.returncode,
            "out": (proc.stdout or "").strip(),
            "err": (proc.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        return {"code": 124, "out": "", "err": f"timer expired after {timeout}s", "timeout": True}
    except FileNotFoundError as exc:
        return {"code": 127, "out": "", "err": f"not found: {exc}"}
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"code": 1, "out": "", "err": f"{type(exc).__name__}: {exc}"}


def adb(*args: str, timeout: int = 120) -> dict[str, Any]:
    return run([ADB, "-s", SERIAL, *args], timeout=timeout)


def adb_shell(command: str, timeout: int = 120) -> dict[str, Any]:
    # Passed as ONE argument: adb joins the argv tail and hands it to the device
    # shell, so pre-splitting would break quoting inside the guest command.
    return adb("shell", command, timeout=timeout)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    import json

    payload.setdefault("ok", code == 0)
    print(json.dumps(payload, default=str))
    return code


def fail(error: str, next_step: str = "", **extra: Any) -> int:
    payload: dict[str, Any] = {"ok": False, "error": error}
    if next_step:
        payload["next"] = next_step
    payload.update(extra)
    return emit(payload, 1)


# --------------------------------------------------------------------------- #
# device lifecycle
# --------------------------------------------------------------------------- #
def device_state() -> dict[str, Any]:
    """What adb currently sees for this serial."""
    listed = run([ADB, "devices"], timeout=60)
    line = ""
    for row in listed["out"].splitlines()[1:]:
        if row.startswith(SERIAL):
            line = row.strip()
    state = line.split()[-1] if line else "absent"
    boot = ""
    if state == "device":
        boot = adb_shell("getprop sys.boot_completed", timeout=30)["out"].strip()
    return {"state": state, "boot_completed": boot, "adb_list": listed["out"]}


def emulator_running() -> bool:
    found = run(["pgrep", "-f", f"qemu-system.*-avd {AVD_NAME}|emulator -avd {AVD_NAME}"], timeout=30)
    return found["code"] == 0 and bool(found["out"])


def ensure_booted(timeout_s: int = BOOT_TIMEOUT_S) -> dict[str, Any]:
    """Make sure a booted device exists, booting one if needed.

    Auto-booting here rather than only in ``boot`` is deliberate: every other
    action would otherwise fail on a cold sandbox with "no device", and the
    agent's next move (call boot, wait ~40 s, retry) is pure ceremony. One place
    that knows how to wait for Android is better than six that assume it.
    """
    state = device_state()
    if state["state"] == "device" and state["boot_completed"] == "1":
        return {"booted": True, "waited_s": 0, "already": True}

    started = 0.0
    if not emulator_running():
        # Widened here as well as in the installer: a sandbox that was re-entered
        # without re-running the installer still needs it, and the emulator
        # refuses to start at all without it (measured).
        run("sudo chmod 666 /dev/kvm 2>/dev/null || true", timeout=30)
        started = time.time()
        cmd = (
            f"nohup {shlex.quote(EMULATOR)} -avd {shlex.quote(AVD_NAME)} "
            f"-no-window -no-audio -no-boot-anim -gpu swiftshader_indirect "
            f"-no-snapshot -accel on -memory 2048 -cores 2 -port {EMULATOR_PORT} "
            f"> {shlex.quote(str(BOOT_LOG))} 2>&1 & echo started"
        )
        run(cmd, timeout=60)
        run([ADB, "start-server"], timeout=60)

    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        st = device_state()
        last = st["state"]
        if st["state"] == "device" and st["boot_completed"] == "1":
            return {
                "booted": True,
                "waited_s": round(time.time() - (started or time.time()), 1),
                "already": False,
            }
        time.sleep(5)

    tail = run(["tail", "-c", "1200", str(BOOT_LOG)], timeout=30)["out"]
    raise EmulatorBootError(
        f"Android did not finish booting within {timeout_s}s (last adb state: {last!r}).",
        tail,
    )


class EmulatorBootError(RuntimeError):
    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def _system_image_path() -> Path:
    """Where sdkmanager puts this image's system.img.

    ``system-images;android-30;google_apis;x86_64`` -> ``<sdk>/system-images/
    android-30/google_apis/x86_64/system.img``. Derived from the image id rather
    than hard-coded, so an operator who overrides ANDROID_SYSTEM_IMAGE gets an
    honest doctor answer instead of a false "missing".
    """
    parts = [p for p in SYSTEM_IMAGE.split(";") if p]
    tail = parts[1:] or ["android-30", "google_apis", "x86_64"]
    return SDK.joinpath("system-images", *tail, "system.img")


def action_doctor(_: argparse.Namespace) -> int:
    checks = {
        "cli_version": CLI_VERSION,
        "sdk_root": str(SDK),
        "sdk_exists": SDK.is_dir(),
        "adb_present": Path(ADB).is_file(),
        "emulator_present": Path(EMULATOR).is_file(),
        "system_image": SYSTEM_IMAGE,
        "system_image_present": _system_image_path().is_file(),
        "avd_ini_present": (AVD_HOME / f"{AVD_NAME}.ini").is_file(),
        "avd_name": AVD_NAME,
        "install_done": INSTALL_DONE.is_file(),
        "kvm_device": Path("/dev/kvm").exists(),
        "kvm_mode": (run(["stat", "-c", "%A", "/dev/kvm"], timeout=20)["out"] or "absent"),
        "user_in_kvm_group": "kvm" in _groups(),
        "emulator_process": emulator_running(),
        "java": (run(["java", "-version"], timeout=30)["err"] or "").splitlines()[:1],
    }
    checks["state"] = device_state()
    checks["ready"] = bool(checks["adb_present"] and checks["emulator_present"]
                           and checks["avd_ini_present"] and checks["kvm_device"])
    checks["installed"] = bool(checks["install_done"])
    note = (
        "Ready. Boot with action='boot', then install_apk/launch/shell/ui/screenshot."
        if checks["ready"] else
        "Not ready: run the installer (action='install'), then action='status' until it reports done."
    )
    return emit({"ok": True, "action": "doctor", "checks": checks, "note": note})


def _groups() -> list[str]:
    res = run(["id", "-nG"], timeout=20)
    return res["out"].split()


def action_status(_: argparse.Namespace) -> int:
    done = INSTALL_DONE.read_text(errors="replace").strip() if INSTALL_DONE.is_file() else ""
    tail = ""
    if INSTALL_LOG.is_file():
        raw = INSTALL_LOG.read_text(errors="replace")
        tail = "\n".join(raw.splitlines()[-25:])
    return emit({
        "ok": True,
        "action": "status",
        "done": bool(done),
        "done_marker": done,
        "log_tail": tail[-1800:],
        "note": (
            "Install finished. Run action='doctor', then action='boot'."
            if done else
            "Still installing. Poll again in ~30s. Do not tell the user to check back."
        ),
    })


def action_install(args: argparse.Namespace) -> int:
    installer = Path(str(HOME_DIR / "bin" / "install_android_sandbox.sh"))
    if not installer.is_file():
        return fail(
            f"installer not found at {installer}",
            "The tool bootstraps it; call action='install' through the android tool, not the CLI directly.",
        )
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    # Detached: a cold SDK download plus a 1.5 GB system image outlives any
    # single sandbox command, and the tool polls `status` instead of blocking.
    run(
        f"nohup bash {shlex.quote(str(installer))} > {shlex.quote(str(INSTALL_LOG))} 2>&1 & echo started",
        timeout=60,
    )
    return emit({"ok": True, "action": "install", "started": True,
                 "log": str(INSTALL_LOG), "poll_with": "status"})


def action_boot(args: argparse.Namespace) -> int:
    try:
        info = ensure_booted(timeout_s=int(args.timeout or BOOT_TIMEOUT_S))
    except EmulatorBootError as exc:
        return fail(str(exc), "Check action='doctor' and the emulator log.", log_tail=exc.log_tail)
    st = device_state()
    props = {}
    for key in ("ro.build.version.release", "ro.build.version.sdk", "ro.product.cpu.abi"):
        props[key] = adb_shell(f"getprop {key}", timeout=30)["out"].strip()
    size = adb_shell("wm size", timeout=30)["out"].strip()
    return emit({"ok": True, "action": "boot", "serial": SERIAL, "state": st["state"],
                 "boot_completed": st["boot_completed"], "boot": info, "props": props,
                 "display": size})


def action_stop(_: argparse.Namespace) -> int:
    adb("emu", "kill", timeout=30)
    killed = run([ADB, "kill-server"], timeout=30)
    run(f"pkill -f 'qemu-system.*{AVD_NAME}' || true", timeout=30)
    return emit({"ok": True, "action": "stop", "killed": True, "detail": killed["out"]})


def action_state(_: argparse.Namespace) -> int:
    st = device_state()
    return emit({"ok": True, "action": "state", "serial": SERIAL,
                 "emulator_process": emulator_running(), **st})


def action_install_apk(args: argparse.Namespace) -> int:
    apk = str(args.apk or "").strip()
    if not apk:
        return fail("apk path is required", "Pass --apk with a path inside the sandbox.")
    if not Path(apk).is_file():
        return fail(
            f"apk not found: {apk}",
            "Download the APK into the sandbox first (sandbox action='fetch_url', or "
            "action='push' to copy one from the sandbox filesystem). Nothing was installed.",
        )
    try:
        ensure_booted(timeout_s=int(args.timeout or BOOT_TIMEOUT_S))
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)
    flags = ["install"]
    if args.reinstall:
        flags.append("-r")
    if args.downgrade:
        flags.append("-d")
    if args.grant:
        flags.append("-g")
    if args.sdcard:
        flags += ["-s", args.sdcard]
    res = adb(*flags, apk, timeout=max(180, int(args.timeout or 600)))
    combined = f"{res['out']}\n{res['err']}".strip()
    success = "Success" in combined or res["code"] == 0
    if not success:
        return fail(
            f"adb install failed: {combined[-600:] or 'no output'}",
            "The APK may be built for another ABI (this device is x86_64) or corrupt.",
            code=res["code"],
        )
    return emit({"ok": True, "action": "install_apk", "apk": apk, "output": combined[-600:]})


def action_uninstall(args: argparse.Namespace) -> int:
    pkg = str(args.package or "").strip()
    if not pkg:
        return fail("package is required", "Pass --package with the application id.")
    res = adb("uninstall", pkg, timeout=180)
    return emit({"ok": True, "action": "uninstall", "package": pkg, "output": res["out"]})


def action_packages(args: argparse.Namespace) -> int:
    res = adb_shell("pm list packages -f", timeout=120)
    rows = []
    for line in res["out"].splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        path, _, pkg = body.rpartition("=")
        rows.append({"package": pkg, "apk": path})
    needle = (args.filter or "").lower()
    if needle:
        rows = [r for r in rows if needle in r["package"].lower()]
    third_party = adb_shell("pm list packages -3", timeout=120)["out"]
    installed_third_party = {ln.strip()[len("package:"):] for ln in third_party.splitlines()
                             if ln.strip().startswith("package:")}
    for row in rows:
        row["installed_by_user"] = row["package"] in installed_third_party
    return emit({"ok": True, "action": "packages", "count": len(rows), "packages": rows})


def action_launch(args: argparse.Namespace) -> int:
    pkg = str(args.package or "").strip()
    if not pkg:
        return fail("package is required", "Pass --package with the application id.")
    try:
        ensure_booted(timeout_s=int(args.timeout or BOOT_TIMEOUT_S))
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)

    # `monkey` resolves the launcher activity for us, which matters because the
    # agent usually has a package name and not an activity class.
    if args.activity:
        component = f"{pkg}/{args.activity}"
    else:
        resolved = adb_shell(
            f"cmd package resolve-activity --brief {shlex.quote(pkg)}", timeout=60
        )["out"].strip().splitlines()
        component = resolved[-1].strip() if resolved else ""
    if not component or "/" not in component:
        return fail(
            f"could not resolve a launcher activity for {pkg}",
            "Check the package name with action='packages', or pass --activity explicitly.",
        )

    if args.url:
        res = adb_shell(
            f"am start -a android.intent.action.VIEW -d {shlex.quote(args.url)} {shlex.quote(component)}",
            timeout=120,
        )
    else:
        res = adb_shell(f"am start -n {shlex.quote(component)}", timeout=120)

    if args.wait:
        # Give the app a moment to draw so a following `ui`/`screenshot` is not
        # photographing a splash screen. Not a sleep-instead-of-wait: the focus
        # check below is the real condition.
        deadline = time.time() + 30
        while time.time() < deadline:
            focus = adb_shell("dumpsys window windows | grep -i mCurrentFocus", timeout=60)["out"]
            if pkg in focus:
                break
            time.sleep(2)
        time.sleep(float(args.settle or 0))

    focus = adb_shell("dumpsys window windows | grep -i mCurrentFocus", timeout=60)["out"].strip()
    running = adb_shell(f"pidof {shlex.quote(pkg)}", timeout=60)["out"].strip()
    return emit({"ok": True, "action": "launch", "package": pkg, "component": component,
                 "start_output": f"{res['out']} {res['err']}".strip()[-400:],
                 "focused_window": focus, "pid": running,
                 "running": bool(running)})


def action_shell(args: argparse.Namespace) -> int:
    if not args.command:
        return fail("command is required", "Pass --command with the shell line to run on the device.")
    try:
        ensure_booted(timeout_s=int(args.timeout or BOOT_TIMEOUT_S))
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)
    res = adb_shell(args.command, timeout=int(args.timeout or 180))
    return emit({
        "ok": res["code"] == 0,
        "action": "shell",
        "command": args.command,
        "exit_code": res["code"],
        "stdout": res["out"][-4000:],
        "stderr": res["err"][-1500:],
    }, 0 if res["code"] == 0 else 1)


def action_tap(args: argparse.Namespace) -> int:
    if args.x is None or args.y is None:
        return fail("x and y are required", "Pass --x and --y in device pixels.")
    res = adb_shell(f"input tap {int(args.x)} {int(args.y)}", timeout=60)
    return emit({"ok": res["code"] == 0, "action": "tap", "x": int(args.x), "y": int(args.y),
                 "detail": res["err"][-300:]})


def action_swipe(args: argparse.Namespace) -> int:
    need = (args.x1, args.y1, args.x2, args.y2)
    if any(v is None for v in need):
        return fail("x1,y1,x2,y2 are required", "Pass all four coordinates in device pixels.")
    duration = int(args.duration or 300)
    res = adb_shell(
        f"input swipe {int(args.x1)} {int(args.y1)} {int(args.x2)} {int(args.y2)} {duration}",
        timeout=60,
    )
    return emit({"ok": res["code"] == 0, "action": "swipe", "duration_ms": duration,
                 "detail": res["err"][-300:]})


# Android key names are the ones an operator expects; the numeric codes are what
# `input keyevent` takes. Only the keys a headless driver actually needs.
_KEYCODES = {
    "home": 3, "back": 4, "call": 5, "endcall": 6, "up": 19, "down": 20,
    "left": 21, "right": 22, "center": 23, "volume_up": 24, "volume_down": 25,
    "power": 26, "camera": 27, "clear": 28, "tab": 61, "space": 62, "enter": 66,
    "del": 67, "menu": 82, "search": 84, "play": 126, "pause": 127, "app_switch": 187,
}


def _keycode(value: Any) -> int | None:
    text = str(value or "").strip().lower().replace("keycode_", "")
    if text.isdigit():
        return int(text)
    return _KEYCODES.get(text)


def action_key(args: argparse.Namespace) -> int:
    code = _keycode(args.keycode)
    if code is None:
        return fail(
            f"unknown key {args.keycode!r}",
            f"Use a number, or one of: {', '.join(sorted(_KEYCODES))}.",
        )
    res = adb_shell(f"input keyevent {code}", timeout=60)
    return emit({"ok": res["code"] == 0, "action": "key", "keycode": code,
                 "detail": res["err"][-300:]})


def action_text(args: argparse.Namespace) -> int:
    if args.text is None:
        return fail("text is required", "Pass --text with the string to type.")
    # `input text` treats spaces and shell metacharacters specially: spaces must
    # be sent as %s, and the whole payload is quoted for the guest shell. Without
    # the %s substitution "hello world" arrives as "hello" and a stray argument
    # error -- measured against a real device.
    payload = str(args.text).replace(" ", "%s")
    payload = re.sub(r"([&;|<>()$`\"'\\*?\[\]#!~])", r"\\\1", payload)
    res = adb_shell(f"input text {payload}", timeout=60)
    return emit({"ok": res["code"] == 0, "action": "text", "typed": str(args.text)[:200],
                 "detail": res["err"][-300:]})


def action_screenshot(args: argparse.Namespace) -> int:
    try:
        ensure_booted(timeout_s=int(args.timeout or BOOT_TIMEOUT_S))
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)
    out = Path(str(args.out or (HOME_DIR / "shots" / f"screen-{int(time.time())}.png")))
    out.parent.mkdir(parents=True, exist_ok=True)
    # exec-out, not `adb shell screencap >`: the shell form mangles the PNG
    # through the console's CRLF translation and produces a corrupt file.
    with open(out, "wb") as fh:
        proc = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                              stdout=fh, stderr=subprocess.PIPE, env=_env(), timeout=120)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        return fail(
            f"screenshot failed (exit {proc.returncode})",
            "Retry; if it repeats, run action='state'.",
            stderr=(proc.stderr or b"").decode("utf-8", "replace")[-400:],
        )
    size = adb_shell("wm size", timeout=30)["out"].strip()
    return emit({
        "ok": True, "action": "screenshot", "path": str(out),
        "bytes": out.stat().st_size, "display": size,
        "note": (
            "The PNG is on the sandbox filesystem. Give it to the user by calling the "
            "sandbox tool with action='download_url' and this path -- do not describe "
            "the screen instead of showing it."
        ),
    })


def action_ui(args: argparse.Namespace) -> int:
    """Dump the live view hierarchy as tappable elements.

    This is what makes tapping possible without a human: the agent reads the
    screen as structured nodes (text, id, bounds, clickable) instead of guessing
    pixel coordinates.
    """
    try:
        ensure_booted(timeout_s=int(args.timeout or BOOT_TIMEOUT_S))
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)

    remote = "/sdcard/window_dump.xml"
    dump = adb_shell(f"uiautomator dump {remote}", timeout=180)
    if "dumped" not in f"{dump['out']}{dump['err']}".lower():
        # A window still animating makes uiautomator bail; one retry is enough.
        time.sleep(2)
        dump = adb_shell(f"uiautomator dump {remote}", timeout=180)
    local = HOME_DIR / "ui.xml"
    local.parent.mkdir(parents=True, exist_ok=True)
    pulled = adb("pull", remote, str(local), timeout=120)
    if not local.is_file():
        return fail(
            f"could not read the view hierarchy: {dump['out'] or dump['err']}".strip()[:400],
            "The screen may be mid-animation. Retry, or use action='screenshot'.",
        )

    try:
        root = ET.fromstring(local.read_text(errors="replace"))
    except ET.ParseError as exc:
        return fail(f"view hierarchy was not valid XML: {exc}",
                    "Retry; if it repeats, use action='screenshot'.")

    nodes: list[dict[str, Any]] = []
    for node in root.iter("node"):
        attrs = node.attrib
        bounds = attrs.get("bounds", "")
        nums = [int(n) for n in re.findall(r"-?\d+", bounds)] or []
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        clickable = attrs.get("clickable") == "true"
        if not (text or desc or clickable):
            continue  # layout scaffolding, not something to act on
        entry: dict[str, Any] = {
            "text": text,
            "desc": desc,
            "id": attrs.get("resource-id", ""),
            "class": (attrs.get("class") or "").split(".")[-1],
            "clickable": clickable,
            "enabled": attrs.get("enabled") == "true",
            "checked": attrs.get("checked") == "true" if attrs.get("checkable") == "true" else None,
        }
        if attrs.get("text") and attrs.get("class", "").endswith("EditText"):
            entry["editable"] = True
        if len(nums) == 4:
            x1, y1, x2, y2 = nums
            entry["bounds"] = [x1, y1, x2, y2]
            entry["center"] = [(x1 + x2) // 2, (y1 + y2) // 2]
        nodes.append(entry)

    focus = adb_shell("dumpsys window windows | grep -i mCurrentFocus", timeout=60)["out"].strip()
    # Only the first N: a full hierarchy is thousands of nodes and the model pays
    # for every token, while the actionable controls are in the first screenful.
    limit = int(args.limit or 60)
    return emit({
        "ok": True, "action": "ui", "count": len(nodes),
        "returned": min(limit, len(nodes)),
        "focused_window": focus,
        "nodes": nodes[:limit],
        "note": (
            "Tap a node with action='tap' --x/--y from its 'center'. Scroll with "
            "action='swipe'. If the screen you expected is not here, the app may "
            "still be loading: action='screenshot' shows the pixels."
        ),
        "truncated": len(nodes) > limit,
    })


def action_push(args: argparse.Namespace) -> int:
    if not args.local or not args.remote:
        return fail("local and remote are required", "Pass --local and --remote.")
    if not Path(args.local).is_file():
        return fail(f"local file not found: {args.local}", "Check the path inside the sandbox.")
    res = adb("push", str(args.local), str(args.remote), timeout=300)
    return emit({"ok": res["code"] == 0, "action": "push", "local": args.local,
                 "remote": args.remote, "output": (res["out"] or res["err"])[-300:]})


def action_pull(args: argparse.Namespace) -> int:
    if not args.remote:
        return fail("remote is required", "Pass --remote with a device path, e.g. /sdcard/file.png.")
    local = str(args.local or (HOME_DIR / "pulled" / Path(args.remote).name))
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    res = adb("pull", str(args.remote), local, timeout=300)
    exists = Path(local).is_file()
    if not exists:
        return fail(
            f"could not pull {args.remote}: {(res['out'] or res['err']).strip()[:300]}",
            "Check the path with action='shell' --command 'ls -l /sdcard'.",
        )
    return emit({"ok": True, "action": "pull", "remote": args.remote, "local": local,
                 "bytes": Path(local).stat().st_size,
                 "note": "Give it to the user with the sandbox tool action='download_url'."})


def action_logcat(args: argparse.Namespace) -> int:
    cmd = "logcat -d"
    if args.filter:
        cmd += f" -s {args.filter}"
    cmd += f" -t {int(args.lines or 200)}"
    res = adb_shell(cmd, timeout=120)
    return emit({"ok": res["code"] == 0, "action": "logcat",
                 "lines": len(res["out"].splitlines()), "log": res["out"][-6000:]})


def action_reset(args: argparse.Namespace) -> int:
    """Wipe the device back to a clean booted state."""
    action_stop(args)
    if args.wipe_data:
        run(f"rm -rf {shlex.quote(str(AVD_HOME))}/{shlex.quote(AVD_NAME)}.avd/*.img "
            f"{shlex.quote(str(AVD_HOME))}/{shlex.quote(AVD_NAME)}.avd/userdata-qemu.img*",
            timeout=120)
        adb("wipe-data", timeout=60)
    try:
        ensure_booted(timeout_s=BOOT_TIMEOUT_S)
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)
    return emit({"ok": True, "action": "reset", "wiped_data": bool(args.wipe_data)})


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Headless Android control (windowless emulator + adb).")
    sub = parser.add_subparsers(dest="action", required=True)

    def add(name: str, *, timeout: bool = True, **kw: Any) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=f"action: {name}")
        if timeout:
            sp.add_argument("--timeout", type=int, default=0,
                            help="Seconds to allow (0 = the action's default).")
        for flag, spec in kw.items():
            sp.add_argument(f"--{flag.replace('_', '-')}", **spec)
        return sp

    add("doctor", timeout=False)
    add("status", timeout=False)
    add("state", timeout=False)
    add("install")
    add("boot")
    add("stop", timeout=False)
    add("install_apk", apk=dict(required=True), reinstall=dict(action="store_true"),
        downgrade=dict(action="store_true"), grant=dict(action="store_true"),
        sdcard=dict(default=""))
    add("uninstall", package=dict(required=True))
    add("packages", filter=dict(default=""))
    add("launch", package=dict(required=True), activity=dict(default=""),
        url=dict(default=""), wait=dict(action="store_true"), settle=dict(type=float, default=2.0))
    add("shell", command=dict(required=True))
    add("tap", x=dict(type=int), y=dict(type=int))
    add("swipe", x1=dict(type=int), y1=dict(type=int), x2=dict(type=int), y2=dict(type=int),
        duration=dict(type=int, default=300))
    add("key", keycode=dict(required=True))
    add("text", text=dict(required=True))
    add("screenshot", out=dict(default=""))
    add("ui", out=dict(default=""), limit=dict(type=int, default=60))
    add("push", local=dict(required=True), remote=dict(required=True))
    add("pull", remote=dict(required=True), local=dict(default=""))
    add("logcat", filter=dict(default=""), lines=dict(type=int, default=200))
    add("reset", wipe_data=dict(action="store_true"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = globals().get(f"action_{args.action}")
    if handler is None:
        return fail(f"unknown action {args.action!r}",
                    f"Valid actions: {', '.join(sorted(k[7:] for k in globals() if k.startswith('action_')))}")
    try:
        return int(handler(args) or 0)
    except EmulatorBootError as exc:
        return fail(str(exc), "Run action='doctor'.", log_tail=exc.log_tail)
    except Exception as exc:  # noqa: BLE001 - one JSON object, always
        return fail(f"{type(exc).__name__}: {exc}", "Report this; the CLI should not raise.")


if __name__ == "__main__":
    sys.exit(main())
