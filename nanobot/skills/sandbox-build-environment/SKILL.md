---
name: sandbox-build-environment
description: Set up toolchains and install packages in restricted sandboxes where apt-get/sudo fail — user-local fallbacks (pip, conda, npm -g to prefix, tarballs, SDKMAN, standalone binaries) plus building APKs (Flutter/native Android), Windows exes, static Linux binaries. Use whenever an install or build fails due to missing root/apt.
metadata: {"nanobot":{"emoji":"🧰","os":["darwin","linux"],"always":false}}
---

# Sandbox Build Environment Skill

Teaches how to get tools installed and builds working when the sandbox has **no sudo**,
**apt-get is blocked/failing**, or the network/proxy restricts system package managers.
The same playbook covers building APKs, `.exe`s, and any task that needs installs.

## Golden rule

Never stop at "apt-get install X failed / permission denied". Treat it as a signal to
**degrade down the ladder below** until something works. Most tools ship as portable
user-space artifacts; you almost never need root.

## Install degradation ladder (try in order)

1. **Already present?** Check before installing anything:
   ```bash
   command -v <tool>; <tool> --version 2>&1 | head -1
   ls /usr/local/bin /opt "$HOME/.local/bin" 2>/dev/null
   ```
   Sandboxes often already have JDKs, Python, Node, git, unzip, curl under `/opt` or `$HOME`.

2. **Language/package managers (user-space, no root):**
   - Python → `pip install --user <pkg>` (or better, a venv):
     ```bash
     python3 -m venv .venv && . .venv/bin/activate && pip install <pkgs>
     # if venv module missing: pip install --user virtualenv && python -m virtualenv .venv
     ```
   - Conda/Mamba (great when many binary deps are needed, fully user-local):
     ```bash
     curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj bin/micromamba
     ./bin/micromamba create -y -p /tmp/env -c conda-forge <pkgs>
     eval "$(/tmp/bin/micromamba shell hook --shell bash)" && micromamba activate /tmp/env
     ```
   - Node → don't `npm i -g` into system; set a user prefix:
     ```bash
     mkdir -p "$HOME/.npm-global" && npm config set prefix "$HOME/.npm-global"
     export PATH="$HOME/.npm-global/bin:$PATH"; npm i -g <pkg>
     ```
   - Rust → `curl https://sh.rustup.rs -sSf | sh -s -- -y` then `source $HOME/.cargo/env`.
   - Go → download the official tarball (see step 3), extract to `$HOME/go-sdk`, add to PATH.

3. **Standalone tarball/binary from upstream (the universal fallback).** Any project that
   ships a portable archive works without a package manager:
   ```bash
   mkdir -p "$HOME/.local/{bin,share}"
   curl -fL <url> -o /tmp/tool.tar.gz      # or .zip / .AppImage / bare binary
   tar -xf /tmp/tool.tar.gz -C "$HOME/.local/share"   # unzip for .zip
   ln -sf "$HOME/.local/share/<tool>/bin/<tool>" "$HOME/.local/bin/<tool>"
   export PATH="$HOME/.local/bin:$PATH"
   ```
   Prefer official GitHub *Releases* assets (look for `linux-x86_64`, `amd64`, `.tar.gz`).
   AppImages: `chmod +x file.AppImage && ./file.AppImage`.

4. **JVM toolchains → SDKMAN (no root):**
   ```bash
   curl -s "https://get.sdkman.io?rcupdate=false" | bash
   source "$HOME/.sdkman/bin/sdkman-init.sh"
   sdk install java 17.0.12-tem        # also: gradle, maven, kotlin, ant
   ```
   Or grab a Temurin JDK tarball directly from Adoptium releases.

5. **Last resort — rebuild what's missing.** If a tiny CLI is only available via apt and
   nothing else works, reimplement the needed behavior with Python/Node/shell, or find an
   equivalent tool that ships portable (e.g. `ripgrep` binary instead of `grep` flags,
   `ffmpeg` static build, `poppler-utils` alternatives like `pdfplumber` via pip).

## Detecting what you're allowed to do (do this first in a new sandbox)

```bash
id                                  # uid? in sudo group?
command -v sudo && sudo -n true 2>&1 # can we sudo non-interactively?
apt-get -v >/dev/null 2>&1 && echo apt-present || echo no-apt
[ -w /usr/local/bin ] && echo writable-usrlocal || echo no-write-system
echo "HOME=$HOME PATH=$PATH"; df -h "$HOME" | tail -1   # disk budget
uname -m                            # x86_64 vs aarch64 -> pick right asset
```
If `sudo -n true` succeeds, apt may still work (`sudo apt-get update`). Otherwise skip straight to the ladder. Always persist env by appending exports to `~/.bashrc` AND re-exporting in each exec call (exec sessions may not reload rc files).

## Building an APK (Android app) without root

APK builds need three things, all installable user-local: a **JDK**, the **Android SDK
(command-line tools)**, and the framework (**Flutter** or Gradle for native). No emulator,
no Android Studio required.

### 1. JDK (SDKMAN or tarball)
```bash
curl -s "https://get.sdkman.io?rcupdate=false" | bash && source "$HOME/.sdkman/bin/sdkman-init.sh"
sdk install java 17.0.12-tem && sdk use java 17.0.12-tem
java -version
```

### 2. Android SDK — command line tools only (no sudo)
Download the zip from Google's distribution page (check the current build number there),
then lay it out exactly as `sdkmanager` expects (`cmdline-tools/latest/bin`):
```bash
export ANDROID_SDK_ROOT="$HOME/android-sdk"
mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
cd /tmp && curl -fLO https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
unzip -q commandlinetools-linux-*_latest.zip
mv cmdline-tools "$ANDROID_SDK_ROOT/cmdline-tools/latest"
export PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH"
yes | sdkmanager --licenses > /dev/null 2>&1 || true
sdkmanager "platform-tools" "build-tools;34.0.0" "platforms;android-34"
```
(Older layout note: the archive extracts to a `cmdline-tools/` folder that must be renamed
to `latest` inside `$ANDROID_SDK_ROOT/cmdline-tools/`, or `sdkmanager` won't find itself.)

### 3a. Flutter app → release APK
```bash
cd ~ && curl -fLO https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.29.3-stable.tar.xz
tar -xf flutter_linux_*-stable.tar.xz -C "$HOME"
export PATH="$HOME/flutter/bin:$PATH"
flutter doctor                # confirm Android toolchain ✓
flutter config --no-analytics
cd /path/to/app && flutter pub get
flutter build apk --release   # output: build/app/outputs/flutter-apk/app-release.apk
```
Use `--debug` for faster iteration, `--split-per-abi` for smaller per-arch APKs.

### 3b. Native Android app → APK
```bash
cd /path/to/project && ./gradlew assembleRelease    # uses SDKMAN gradle or wrapper
# output: app/build/outputs/apk/release/app-release.apk
```
If there's no gradle wrapper: `sdk install gradle 8.7` (SDKMAN) then `gradle assembleRelease`.

### APK troubleshooting
- **"SDK location not found"** → export `ANDROID_SDK_ROOT`/`ANDROID_HOME` AND write
  `sdk.dir=$HOME/android-sdk` into the project's `local.properties`.
- **License errors** → rerun `yes | sdkmanager --licenses`.
- **Java version mismatch** → Flutter/AGP want JDK 17; `sdk use java 17.0.12-tem`.
- **No space** → point SDK/Gradle caches somewhere big:
  `export GRADLE_USER_HOME=/tmp/gradle ANDROID_SDK_ROOT=/tmp/android-sdk`.
- **Timeout on big downloads** → run the download with `yield_time_ms` background exec and
  poll, or `curl -C -` to resume.

## Other build targets (same philosophy)

- **Windows `.exe` from Linux**: cross-compile with MinGW-w64 (`x86_64-w64-mingw32-gcc`) —
  install via a portable toolchain or `pip install ziglang` then `zig cc -target x86_64-windows-gnu`;
  Rust: `rustup target add x86_64-pc-windows-gnu` (mingw libs) or `-msvc` via Docker-free `cargo-xwin`.
- **Static Linux binary** (runs anywhere): prefer musl/static linking — Rust `cargo build --target x86_64-unknown-linux-musl`, Go `CGO_ENABLED=0 go build` (already static), C with `gcc -static`.
- **Python → exe**: `pip install pyinstaller && pyinstaller --onefile app.py`.
- **Docker unavailable**: most CI images forbid it — build natively with the steps above instead.

## Persistence & hygiene
- Every install goes under `$HOME` or `/tmp` (never `/usr` unless writable).
- Re-export PATH in each exec call or append to `~/.bashrc`; assume fresh shells.
- Cache dirs: `GRADLE_USER_HOME`, `PIP_CACHE_DIR`, `ANDROID_SDK_ROOT` — set them to roomy paths.
- Verify after each install (`--version`) before proceeding; fail fast and switch method.
- Report which method succeeded so future turns reuse it.
