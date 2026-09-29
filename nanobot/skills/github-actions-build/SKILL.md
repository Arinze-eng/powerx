---
name: github-actions-build
description: >-
  Build distributable artifacts — Android APK, Windows EXE, iOS/iPad IPA, Linux .deb — or run a test
  suite using GitHub Actions runners via the build_artifact TOOL. Use whenever the user asks to
  compile/package one of those from their project source. This is the PREFERRED path for apk/exe/ipa/deb
  built from source — try it first, because the runners already carry the Android SDK / Xcode / Windows
  toolchains. If this path is unavailable (GITHUB_BUILD_TOKEN unset) or its run cannot be made green,
  fall back to installing the toolchain and building in the sandbox (see the sandbox-build-environment
  skill); never refuse the build. The agent creates a throwaway repo on the dedicated build account,
  pushes the project, runs the right workflow, watches it, fixes errors, downloads the artifact, then
  deletes the repo.
metadata: {"nanobot":{"emoji":"📦","os":["darwin","linux"],"always":true}}
---

# GitHub Actions Build Tool

Build a real installable/distributable artifact for the user on **GitHub Actions
runners** (which have the Android SDK, Xcode, and Windows toolchains the default
sandbox image lacks). Everything runs through the **`build_artifact` tool** — an
invokable capability, not a script you shell out to. It talks to GitHub with the
dedicated build account's token (`GITHUB_BUILD_TOKEN`, configured on the backend),
so you never handle credentials yourself.

## When to use this

The user wants a shippable **APK / EXE / iPA / DEB**, or a **CI test run**, produced from
their project source. Try this first: route straight here rather than hand-installing a
toolchain, because the runners already have one. Only if this path is genuinely
unavailable — the tool is disabled because `GITHUB_BUILD_TOKEN` is unset, or every fix
still leaves the run red — do you *fall back* to the **sandbox build** recipe in the
`sandbox-build-environment` skill. (Reverse-engineering an *existing* APK binary stays in
the sandbox via the apk_toolchain actions; that is editing a binary, not building from
source.)

## ⛔ HARD RULE — never refuse to build an artifact

An **APK / EXE / iPA / DEB / CI-test** asked for by the user must be **delivered**, by one
of two paths. Refusing is never one of them.

1. **Preferred path:** the `build_artifact` tool (GitHub Actions runners). Use it when it is
   enabled.
2. **Fallback path:** build it in the sandbox, following the install ladder in the
   **`sandbox-build-environment`** skill — JDK, Gradle and the Android command-line tools all
   install user-space with no root. Then hand the user the produced file.

You MUST NOT:

- **Refuse** the build, or tell the user the artifact "cannot be built here", because
  `GITHUB_BUILD_TOKEN` is unset, because CI failed, or because the sandbox image has no
  Android SDK / Xcode / Windows toolchain. None of those is a blocker — the first two send you
  to the sandbox, and a missing toolchain is an install step.
- Tell the user to "ask the operator to configure `GITHUB_BUILD_TOKEN`" and stop there. Say
  it, then **build it in the sandbox anyway**.
- Hardcode or ask for a token. If you need it, it is an operator setting, not a user task.

Only after **both** paths have actually been attempted and failed may you report a blocker —
and then you state the failing step and its real error message, plus what you tried. "The
sandbox has no Android SDK" on its own is not a result; it is the setup step you skipped.

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
     project dir, then re-run `push` + `trigger` + `watch`. Repeat a few times; if it stays
     red, switch to the sandbox build rather than looping.
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

## When CI is not an option — build in the sandbox instead

`build_artifact.enabled()` is false without `GITHUB_BUILD_TOKEN`, and a CI run can stay red for
reasons outside the project (runner image drift, a workflow GitHub rejects). In either case the
answer is the **sandbox build**, not a refusal:

- APK → install a user-space JDK 17 + Android `cmdline-tools` + `sdkmanager` platform/build-tools,
  then `./gradlew assembleDebug` (or `flutter build apk`). Check disk first: the SDK lands around
  2–3 GB, so set `GRADLE_USER_HOME` and `ANDROID_SDK_ROOT` under a roomy path.
- EXE → `pip install pyinstaller` (the emulator path already used for MT5 Wine work is the local
  analogue when the target must run under Wine).
- iPA → the sandbox is Linux, so an unsigned `.app`/`.ipa` is not reachable in-sandbox; this is the
  one target where GitHub Actions (macos runner) is genuinely required. Say so, and offer the
  project layout / simulator-free alternatives.
- DEB → `dpkg-deb --build --root-owner-group`, or `fakeroot` if not writable.

The step-by-step install ladder lives in the **`sandbox-build-environment`** skill; follow it
before concluding anything is missing.

## Constraints to state honestly

- **iPA**: produces an unsigned `.app` (or unsigned `.ipa` with `sdk=iphoneos`). Real device
  signing needs the user's Apple Developer cert/profile — say so; offer the unsigned build.
- **APK signing**: debug/unsigned-release only unless the user provides keystore secrets. A
  sandbox-built debug APK is a normal, installable result — deliver it.
- If `GITHUB_BUILD_TOKEN` isn't configured (tool disabled), state it once as context — the CI
  account is unavailable — and then **build the artifact in the sandbox**. Do **not** hardcode a
  token, and do **not** tell the user the artifact cannot be produced.
