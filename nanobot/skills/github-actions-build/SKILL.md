---
name: github-actions-build
description: >-
  Build software artifacts for the user in the cloud via GitHub Actions instead of the local sandbox.
  Supports Android APK, Windows EXE, iOS/iPad IPA, DEB packages, and running test suites. The agent
  auto-creates a throwaway GitHub repo under the dedicated build account (william165-bot), pushes the
  user's project files, writes the matching workflow, triggers it via workflow_dispatch, polls until it
  finishes, auto-fixes build errors by pushing a fix and re-running, downloads the built artifact, then
  deletes the repo. Use whenever the user wants an apk/exe/ipa/deb/test built, a compiled/installable
  artifact produced, or an iOS build (which needs macOS runners the local sandbox lacks).
metadata: {"nanobot":{"emoji":"📦","os":["darwin","linux"],"always":false,"requires":{"bins":["gh","curl"]}}}
---

# GitHub Actions Build Tool

Build a real artifact for the user using **GitHub Actions runners** (ubiquitous, self-updating,
and able to build macOS/iOS/Windows things the Linux sandbox cannot). Everything happens on the
**dedicated build account** `william165-bot`; nothing touches the user's own PowerX account.

## When to prefer this over the local sandbox builder

- User asks for an **APK / EXE / iPA / DEB / "build it for me"** and wants a polished artifact.
- Anything Android (Gradle/SDK), **iOS/iPad (needs macOS)**, Windows EXE, or a test run against CI.
- Local sandbox lacks Android SDK, Xcode, or Windows toolchains.
Use the `sandbox-build-environment` skill only for quick local compile checks of small utilities.

## Critical setup — the dedicated account token

The build account is **`william165-bot`**. Its PAT must be available in the environment:

```bash
export GITHUB_BUILD_TOKEN=<the dedicated william165-bot PAT>   # scopes: repo, workflow, delete_repo
gh auth login --with-token <<< "$GITHUB_BUILD_TOKEN" || true    # if `gh` needs a token
```

If `GITHUB_BUILD_TOKEN` is not set, ask the user for it or read it from the workspace secrets —
**never hardcode, never commit, never echo it** in the conversation.

## The flow (run these `scripts/github_build.py` steps in order)

1. **Pick a repo name** — anything of your choosing (short, prefix with `build-`, e.g. `build-cats-app`).
2. **Create** a private throwaway repo:
   ```bash
   python scripts/github_build.py create --name build-cats-app
   ```
3. **Push** the user's project folder:
   ```bash
   python scripts/github_build.py push --repo william165-bot/build-cats-app --src /path/to/project
   ```
4. **Add the matching workflow** (see table below):
   ```bash
   python scripts/github_build.py add-workflow --repo william165-bot/build-cats-app --type build-apk
   ```
5. **Trigger** the workflow (pass inputs as JSON):
   ```bash
   python scripts/github_build.py trigger --repo william165-bot/build-cats-app \
       --workflow build-apk.yml \
       --inputs '{"gradle_task":"assembleRelease","module":"app"}'
   ```
6. **Watch** until it completes:
   ```bash
   python scripts/github_build.py watch --repo william165-bot/build-cats-app --run <RUN_ID> --timeout 1800
   ```
   - `0` → completed successfully → go to step 8.
   - `2` → **build failed** → go to step 7 (fix loop).
7. **Fix loop** (build errors):
   ```bash
   # 1) pull the failing log (gh needs the token exported as GH_TOKEN)
   export GH_TOKEN="$GITHUB_BUILD_TOKEN"
   gh run view <RUN_ID> --repo william165-bot/build-cats-app --log-failed
   # 2) diagnose + fix the code in the local project folder
   # 3) re-push the fixed files and re-trigger, then watch again
   python scripts/github_build.py fix-push --repo william165-bot/build-cats-app --src /path/to/project --msg "fix: <what>"
   python scripts/github_build.py trigger --repo william165-bot/build-cats-app --workflow build-apk.yml --inputs '<same inputs>'
   python scripts/github_build.py watch --repo william165-bot/build-cats-app --run <NEW_RUN_ID>
   ```
   Repeat until success or a hard blocker (then report the error plainly to the user).
8. **Download** the artifact:
   ```bash
   python scripts/github_build.py download --repo william165-bot/build-cats-app \
       --run <RUN_ID> --dest ./out --artifact-name apk
   ```
9. **Cleanup** — always delete the throwaway repo when done:
   ```bash
   python scripts/github_build.py delete --repo william165-bot/build-cats-app
   ```
10. **Report** the artifact path(s) to the user. If they asked to also push the result to their
    PowerX repo or deploy to Northflank, follow the `vercel-deployment` / repo-push flow next.

## Workflow type → inputs table

| `--type`        | Runner            | Default `inputs` (override as needed)                    |
|-----------------|-------------------|----------------------------------------------------------|
| `build-apk`     | ubuntu-latest     | `{"gradle_task":"assembleDebug","java_version":"17"}`    |
| `build-exe`     | windows-latest    | `{"build_command":"py -m PyInstaller --onefile app.py"}` |
| `build-ipa`     | macos-latest      | `{"scheme":"<YourScheme>","sdk":"iphonesimulator"}`      |
| `build-deb`     | ubuntu-latest     | `{}`   (uses DEBIAN/ or debian/ control if present)      |
| `run-tests`     | ubuntu-latest     | `{"test_command":"npm test","language":"node-20"}`       |

## Notable constraints

- **iPA/iOS**: builds an unsigned `.app` (`.ipa` if `sdk=iphoneos`). Signing/distribution to a
  device requires the user's Apple Developer cert/profile — state this clearly; offer to hand
  over the unsigned IPA + a signing path.
- **APK signing**: the template produces a debug or unsigned-release APK. For a signed store APK,
  the user must supply `keystore`/`keyalias` credentials as repo/environment secrets.
- The workflow templates live under `scripts/workflows/` and are copied into each temp repo, so
  editing a template here affects all future builds.
- **Always delete the temp repo** (step 9). Do not leave throwaway repos behind.

## References & templates

- Workflow YAML templates: `scripts/workflows/build-apk.yml`, `build-exe.yml`, `build-ipa.yml`, `build-deb.yml`, `run-tests.yml`.
- Orchestration CLI: `scripts/github_build.py` (see its `--help` / this file's command list).