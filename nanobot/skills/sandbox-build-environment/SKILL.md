---
name: sandbox-build-environment
description: >-
  Set up toolchains and install packages in restricted sandboxes where apt-get/sudo fail — user-local
  fallbacks (pip, conda, npm -g to prefix, tarballs, SDKMAN, standalone binaries) plus compiling small
  local utilities/static Linux binaries. Use whenever an install or build fails due to missing root/apt.
  NOTE that building distributable APK/EXE/iPA/DEB artifacts from a project's source is NOT done here —
  that MUST use the `github-actions-build` skill (GitHub Actions runners), since the sandbox lacks
  Android SDK, Xcode, and Windows toolchains.
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

## ⛔ HARD RULE — NEVER build APK / EXE / iPA / DEB here

Building a **shippable Android APK, Windows EXE, iOS/iPad IPA, or .deb** from project source is
**FORBIDDEN in the sandbox — no exceptions.** This includes:

- Installing JDK / Gradle / Android SDK / Android command-line tools / Flutter / Xcode /
  CocoaPods / PyInstaller / dpkg toolchains in the sandbox **to build or ship** any of these
  artifacts.
- Running `./gradlew`, `gradle`, `flutter build`, `xcodebuild`, `pyinstaller`, `dpkg-deb`, or
  equivalent in the sandbox to produce a deliverable artifact.

Such artifacts **MUST** go through the **`github-actions-build`** skill / the `build_artifact`
tool (GitHub Actions runners, which have the real Android SDK / Xcode / Windows toolchains).
If that tool is unavailable, **refuse** the build and tell the user the operator must configure
`GITHUB_BUILD_TOKEN` on the backend — never fall back to sandbox building.

This skill stays responsible ONLY for:
- Installing user-space toolchains/packages to get a local **dev environment** working
  (e.g. compiling a small Linux utility, running tests locally, installing language managers),
  and
- Preparing/scaffolding a project so the user can inspect it **before** it is shipped by
  GitHub Actions. A local JDK/Flutter install is never a substitute for the shipped artifact.

### Other native/package targets
Windows `.exe`, iOS `.ipa`, and `.deb` are likewise **GitHub Actions only** (windows-latest,
macos-latest, ubuntu-latest runners respectively). Static Linux binaries and Go/Rust/C cross-
compilation for genuinely local, non-deliverable helper use remain fine here.

## Persistence & hygiene
- Every install goes under `$HOME` or `/tmp` (never `/usr` unless writable).
- Re-export PATH in each exec call or append to `~/.bashrc`; assume fresh shells.
- Cache dirs: `GRADLE_USER_HOME`, `PIP_CACHE_DIR`, `ANDROID_SDK_ROOT` — set them to roomy paths.
- Verify after each install (`--version`) before proceeding; fail fast and switch method.
- Report which method succeeded so future turns reuse it.
