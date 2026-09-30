# Android in a Sandbox

`android_sandbox` runs a real Android app inside the execution sandbox and drives
it entirely from the shell: install an APK, launch it, tap and type, read the live
view hierarchy, take screenshots, move files in and out, read logcat. No GUI and
nobody touching a screen.

It works on both sandbox backends, but they are not equally suited to it, and the
difference is measured below rather than assumed.

## What the tool gives you

| Action | What it does |
|---|---|
| `install` | Installs the emulator, `adb`, an Android 11 system image and an AVD inside the sandbox. Detached: poll `status` until it reports done |
| `doctor` | What is installed, whether the emulator binary loads, and which acceleration the host got |
| `boot` / `state` / `stop` / `reset` | Start, inspect, stop, or wipe the device |
| `install_apk` / `uninstall` / `packages` | Install an APK (`--grant` to pre-grant runtime permissions), remove it, list what is installed |
| `launch` | Start an app (and optionally wait for it) |
| `ui` | The live view hierarchy as structured nodes with text, description, bounds and a tappable `center` |
| `tap` / `swipe` / `key` / `text` | Programmatic input: coordinates, gestures, keycodes, typing |
| `screenshot` | The screen as a PNG inside the sandbox |
| `push` / `pull` / `shell` | Files in and out, and arbitrary `adb shell` |
| `logcat` | Why an app crashed |

The agent's loop is deliberate: `ui` to read the screen, decide from the nodes,
`tap` a node's `center`, `ui` again. Guessing pixels is the fallback, not the plan.

## The two backends, measured

Measured 2026-09-30 in one session, same revision, same ledger of steps.

| | Freestyle VM | Tenki container |
|---|---|---|
| Host | 8 GB, 4 vCPU, kernel 6.1.102 | 4 GB, 2 vCPU, kernel 6.18.29 |
| `/dev/kvm` | present, nested virtualisation | **absent** (no `vmx`/`svm`, no `/lib/modules`) |
| Acceleration | hardware (`-accel on`) | software (`-accel off`, TCG) |
| Guest RAM / cores | 2048 MB / 2 | 1536 MB / 2 (a smaller guest, because the host swaps) |
| Emulator boot | **~50 s** | **~20 min** |
| Emulator RSS | ~2.6 GB | ~3.4 GB on a 3.9 GB host |
| Ledger | 26/27 then 17/17 on the revised scripts | see below |

The ledger is: install → doctor → boot → install an APK (NewPipe) → launch → `ui`
→ tap a node → swipe → keys → type into Settings search → screenshot → push/pull
round-trip → logcat → uninstall → stop. On Freestyle every step passed
(`Performing Streamed Install / Success`, an 11-node `ui` dump, a tap on a real
node, a 44 KB screenshot, a push/pull round-trip whose file content matched). The
single miss in the first pass was the test harness's own attempt to download a
screenshot to a path outside its workspace, not the tool.

On the 4 GB container the emulator is verified booting to
`sys.boot_completed=1` under software emulation, and it installs and runs an app
from that state. It is simply **minutes slower**, which is why the tool never
pretends a slow host is a broken one.

## What a host must provide

1. **The emulator's runtime libraries.** A bare image fails at the first load:
   `emulator: error while loading shared libraries: libX11.so.6`. The launcher
   links X11 even with `-no-window`, and it `dlopen()`s more at runtime
   (`libX11-xcb.so.1`, `libpulse.so.0`) which `ldd` cannot see. The installer
   installs the known set, then loops on `ldd`'s own "not found" list, then proves
   the binary loads. `doctor` reports `emulator_missing_libs` and
   `emulator_load_error` if that ever regresses.
2. **`/dev/kvm` for a usable speed.** Without it the emulator still runs, in
   software. Nothing refuses, but a boot goes from seconds to tens of minutes.
3. **~3 GB of disk for the image** (~4 GB with the SDK) and enough RAM for the
   guest plus the emulator's own overhead. Below ~5 GB of host RAM the CLI hands
   the guest 1536 MB instead of 2048 MB, because a larger guest leaves the host
   swapping and the boot never finishes.
4. **No host kernel module.** That is why this is an emulator and not Waydroid:
   Waydroid needs `binder_linux`, and a provider kernel that ships neither that
   nor `ashmem_linux` and has no module build headers cannot provide it. The
   emulator carries its own kernel.

## Reading `boot` when the host is slow

`boot` is bounded by the sandbox's per-command limit, which a software boot
exceeds. So on a host without hardware acceleration `boot` answers

```json
{"ok": true, "action": "boot", "booting": true, "accel": "off", "note": "..."}
```

and the agent polls `state` until `boot_completed` is `"1"`. That is an answer,
not a failure: `state` reports `emulator_process`, `accel`, and the emulator log
tail so a still-starting device is distinguishable from a dead one. `boot` only
fails when the emulator has actually exited or its loader error is known.

## Installing by URL

The tool is a thin forwarder: it holds no Android code. Each call bootstraps
`scripts/android_cli.py` and `scripts/install_android_sandbox.sh` out of the repo
into the sandbox, resolving `main` to a commit SHA first (the sandbox's egress
path caches `raw.githubusercontent.com` by path, so a branch URL can serve a
stale file), and refuses to run a CLI whose `CLI_VERSION` does not match the
tool's `_CLI_VERSION`. Bump both together when the contract changes.
