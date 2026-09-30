---
name: sandbox-build-environment
description: >-
  Set up toolchains and install packages in restricted sandboxes where apt-get/sudo fail — user-local
  fallbacks (pip, conda, npm -g to prefix, tarballs, SDKMAN, standalone binaries) plus compiling small
  local utilities/static Linux binaries. Use whenever an install or build fails due to missing root/apt,
  AND as the fallback recipe when a distributable artifact (APK/deb/EXE) cannot be produced through
  `github-actions-build` — install the toolchain here and build it in the sandbox rather than refusing.
metadata: {"nanobot":{"emoji":"🧰","os":["darwin","linux"],"always":false}}
---

# Sandbox Build Environment Skill

Teaches how to get tools installed and builds working when the sandbox has **no sudo**,
**apt-get is blocked/failing**, or the network/proxy restricts system package managers.
The same playbook covers building APKs and `.deb`s locally when the cloud builder is not
an option.

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

## ✅ Building a distributable artifact in the sandbox — allowed, and often the answer

Prefer the **`github-actions-build`** skill / `build_artifact` tool first: its runners already
have the Android SDK, Xcode and Windows toolchains. But when that path is unavailable
(`GITHUB_BUILD_TOKEN` unset) or its run cannot be made green, you **install the toolchain here
and build the artifact in the sandbox**. An artifact the user asked for is delivered from
wherever it can be built — a sandbox-built debug APK is a normal result, not a downgrade.

**Never refuse these builds and never tell the user the sandbox "cannot" build an APK.**
The default image merely ships without the Android SDK; that is an install step below.

### Android APK — full user-space recipe

Disk first: JDK + SDK + build-tools land around 2.5–3.5 GB, so point the caches at a roomy
path and check `df -h "$HOME"` before starting.

```bash
# 0. Budget + base tools
df -h "$HOME" | tail -1
command -v unzip curl git >/dev/null || echo "install zip/curl equivalents first"

# 1. JDK 17 (Temurin, no root)
curl -fL -o /tmp/jdk.tar.gz \
  "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"
mkdir -p "$HOME/.local/jdk" && tar -xzf /tmp/jdk.tar.gz -C "$HOME/.local/jdk" --strip-components=1
export JAVA_HOME="$HOME/.local/jdk"; export PATH="$JAVA_HOME/bin:$PATH"
java -version   # verify before continuing

# 2. Android command-line tools + SDK (bump CLT_VERSION when Google rotates the file)
export ANDROID_SDK_ROOT="$HOME/android-sdk"; export ANDROID_HOME="$ANDROID_SDK_ROOT"
mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
curl -fL -o /tmp/clt.zip \
  "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
unzip -q /tmp/clt.zip -d "$ANDROID_SDK_ROOT/cmdline-tools"
mv "$ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools" "$ANDROID_SDK_ROOT/cmdline-tools/latest"
export PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH"

# 3. Licenses + the components a Gradle Android build needs
export GRADLE_USER_HOME="$HOME/.gradle"
yes | sdkmanager --licenses >/dev/null 2>&1 || true
sdkmanager --install "platform-tools" "platforms;android-34" "build-tools;34.0.0"

# 4. Gradle if the project has no wrapper (otherwise use ./gradlew)
#    sdkman:  source "$HOME/.sdkman/bin/sdkman-init.sh" && sdk install gradle 8.7
curl -fL -o /tmp/gradle.zip https://services.gradle.org/distributions/gradle-8.7-bin.zip
unzip -q /tmp/gradle.zip -d "$HOME/.local"; export PATH="$HOME/.local/gradle-8.7/bin:$PATH"

# 5. Build from the project root (add sdk.dir so Gradle stops hunting for the SDK)
cd "$HOME/workspace/<project>"
echo "sdk.dir=$ANDROID_SDK_ROOT" > local.properties
./gradlew assembleDebug --no-daemon        # no wrapper: gradle assembleDebug --no-daemon

# 6. Hand the user the artifact  ->  see "Delivering the finished file" below
find . -name '*.apk' -newermt '-1 hour' | head
```

### Delivering the finished file — onlyfiles, one link, permanently valid

The build is not the deliverable; the **link** is. Publish it with one call on the
sandbox tool and hand over exactly what comes back:

```
{"action":"download_url","path":"<path inside the sandbox workspace>"}   # e.g. app-debug.apk
```

* The returned link is a permanent **`https://onlyfiles.com/…`** page URL (uploads use
  `expire=0`; `https://onlyfiles.com/api` is the documented contract) — or
  `https://files.catbox.moe/…` for files over ~100 MB. Nothing else in the tool result is
  a link: the internal `/dl/` token lives 300 s and the `<deployment-host>/f/<id>` form
  only works while that host answers.
* **Never** hand over a `/f/<id>` link, a sandbox preview or signed URL, a raw transfer
  token, or a cloud-drive/share link instead. They are dead links for the user and they
  are what "the link is not working" reports are made of.
* **Never** deliver a sandbox path as if it were the file — the user cannot read it, and
  the sandbox is recycled.
* Deliver once, at the end, plainly naming the artifact and any caveat (a debug-signed APK
  must be installed after uninstalling the original).

Flutter projects: install the Flutter tarball (`flutter_linux_*stable.tar.xz`, add
`$HOME/flutter/bin` to PATH, `flutter config --android-sdk "$ANDROID_SDK_ROOT"`,
`yes | flutter doctor --android-licenses`), then `flutter build apk --debug`.

If a step fails, work **down the ladder** (micromamba for binary deps, a different
Adoptium/Gradle build, `--offline` from a warm `GRADLE_USER_HOME`) and report the exact
command and error at the point you truly could not proceed — never "the sandbox has no
Android SDK" as an end state.

### Other native/package targets

- **`.deb`** → `dpkg-deb --build --root-owner-group <dir> out.deb`, with `fakeroot` when the
  tree is not writable. Perfectly doable in-sandbox.
- **Windows `.exe`** → build with PyInstaller (or MinGW cross-compile) here; the Wine stack the
  MT5 work already installs is how you *run and verify* it locally.
- **iOS/iPad `.ipa`** → the sandbox is Linux, so an Xcode build genuinely is not reachable
  here; this one target needs the `build_artifact` (macos runner) path. Say exactly that — and
  only that — instead of a blanket refusal.
- Static Linux binaries and Go/Rust/C cross-compilation for local, non-deliverable helper use
  remain fine here.

This skill stays responsible for both the local **dev-environment** setup and the
**sandbox artifact build** described above.

## Persistence & hygiene
- Every install goes under `$HOME` or `/tmp` (never `/usr` unless writable).
- Re-export PATH in each exec call or append to `~/.bashrc`; assume fresh shells.
- Cache dirs: `GRADLE_USER_HOME`, `PIP_CACHE_DIR`, `ANDROID_SDK_ROOT` — set them to roomy paths.
- Verify after each install (`--version`) before proceeding; fail fast and switch method.
- Report which method succeeded so future turns reuse it.
