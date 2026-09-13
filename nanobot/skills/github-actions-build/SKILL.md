---
name: github-actions-build
description: >-
  Build distributable artifacts — Android APK, Windows EXE, iOS/iPad IPA, Linux .deb — or run a test
  suite using GitHub Actions runners via the build_artifact TOOL. Use whenever the user asks to
  compile/package one of those from their project source. This is the MANDATORY path for apk/exe/ipa/deb
  built from source — never attempt those in the sandbox (no Android SDK / Xcode / Windows toolchain).
  The agent creates a throwaway repo on the dedicated build account, pushes the project, runs the right
  workflow, watches it, fixes errors, downloads the artifact, then deletes the repo.
metadata: {"nanobot":{"emoji":"📦","os":["darwin","linux"],"always":true}}
---

# GitHub Actions Build Tool

Build a real installable/distributable artifact for the user on **GitHub Actions
runners** (which have the Android SDK, Xcode, and Windows toolchains the sandbox lacks).
Everything runs through the **`build_artifact` tool** — an invokable capability, not a
script you shell out to. It talks to GitHub with the dedicated build account's token
(`GITHUB_BUILD_TOKEN`, configured on the backend), so you never handle credentials yourself.

## When to use this

The user wants a shippable **APK / EXE / iPA / DEB**, or a **CI test run**, produced from
their project source. Do NOT try gradle/flutter/xcode/pyinstaller/dpkg inside the sandbox —
route straight here. (Reverse-engineering an *existing* APK binary stays in the sandbox via
the apk_toolchain actions; that is editing a binary, not building from source.)

## ⛔ HARD RULE — this is the ONLY allowed way to build these artifacts

Building any **APK / EXE / iPA / DEB / CI-test** from project source is **only** permitted
through the `build_artifact` tool (GitHub Actions runners). You MUST:

- **NEVER** install, download, or configure Android SDK, Gradle, JDK, Flutter, Xcode,
  CocoaPods, PyInstaller, dpkg, or any other build toolchain **in the sandbox** for the
  purpose of producing one of these artifacts.
- **NEVER** run `gradle`, `./gradlew`, `flutter build`, `xcodebuild`, `pyinstaller`,
  `dpkg-deb`, or equivalent **in the sandbox** to ship an artifact to the user.
- If the `build_artifact` tool is **unavailable or errors**, you MUST **refuse** to build the
  artifact and clearly explain: "Building {APK/EXE/iPA/DEB} is only possible through the
  build_artifact (GitHub Actions) tool, which is currently unavailable — ask the operator to
  configure GITHUB_BUILD_TOKEN on the backend." You do **NOT** fall back to sandbox building.
- The sandbox has no Android SDK / Xcode / Windows toolchains, so sandbox builds would both
  violate this policy and fail. Never waste the user's time attempting them.

## How to drive `build_artifact`

Each call takes `action=` plus a few fields. The repo lives under the build account by
default, so `repo=` may be just the name. Drive it step-by-step, or use the one-shot `build`.

One-shot (recommended when the project dir is ready):
```
build_artifact action=build  name=<any-short-name>  type=apk|exe|ipa|deb|test
                 source_dir=<project dir in workspace>  inputs_json={"...workflow inputs..."}
```
That performs create → push → add_workflow → trigger → watch and returns the run id + result.

Step-by-step (use when you must inspect between steps):
1. `build_artifact action=create name=build-myapp` → note returned `repo=owner/name`.
2. `build_artifact action=push repo=<repo> source_dir=/path/to/project`
3. `build_artifact action=add_workflow repo=<repo> type=apk`   (apk|exe|ipa|deb|test)
4. `build_artifact action=trigger repo=<repo> type=apk inputs_json='{"gradle_task":"assembleRelease"}'`
   → returns `run <id>`.
5. `build_artifact action=watch repo=<repo> run_id=<id>`
   - SUCCESS → go to 6.
   - FAILURE → it returns the **failed log tail**. Diagnose, fix the files in your local
     project dir, then re-run `push` + `trigger` + `watch`. Repeat until green or blocked.
6. `build_artifact action=download repo=<repo> run_id=<id> dest_dir=build-out`
   → artifact lands under `build-out/<artifact>/`; give the user the path(s).
7. `build_artifact action=delete repo=<repo>` — ALWAYS clean up the throwaway repo.

## Workflow inputs (`inputs_json`) per type

| type | runner | key inputs (defaults shown) |
|------|--------|------------------------------|
| `apk`  | ubuntu  | `{"gradle_task":"assembleDebug","java_version":"17","module":""}` |
| `exe`  | windows | `{"build_command":"pyinstaller --onefile app.py"}` |
| `ipa`  | macos   | `{"scheme":"<YourScheme>","sdk":"iphonesimulator","configuration":"Release"}` |
| `deb`  | ubuntu  | `{}` (uses DEBIAN/control or debian/ if present) |
| `test` | ubuntu  | `{"test_command":"npm test","language":"node"}` |

## Constraints to state honestly

- **iPA**: produces an unsigned `.app` (or unsigned `.ipa` with `sdk=iphoneos`). Real device
  signing needs the user's Apple Developer cert/profile — say so; offer the unsigned build.
- **APK signing**: debug/unsigned-release only unless the user provides keystore secrets.
- If `GITHUB_BUILD_TOKEN` isn't configured (tool disabled/unavailable), **refuse to build the
  artifact in the sandbox**. Tell the user: "The build_artifact (GitHub Actions) tool is
  unavailable because GITHUB_BUILD_TOKEN is not set — ask the operator to configure it on the
  backend." Do **not** hardcode a token and do **not** attempt a sandbox build.
