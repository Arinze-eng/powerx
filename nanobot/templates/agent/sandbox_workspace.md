## Sandbox Workspace Map (READ THIS BEFORE ANY SANDBOX TASK)

You work in TWO separate filesystems. Never confuse them:

1. **Agent workspace** — `{{ agent_workspace_path }}` on the gateway host.
   Reached by the `exec`, `read_file`, and `write_file` tools. Holds memory,
   skills, and user-facing deliverables. NOT visible to sandbox commands.
2. **Sandbox workspace** — `{{ sandbox_workspace_dir }}` inside the isolated
   execution environment (`{{ sandbox_backend }}` backend). Reached ONLY via
   the `novita_sandbox` tool. Relative paths passed to that tool resolve under
   this directory automatically. Every run command
   starts with this as its working directory.

### The sandbox layout convention (follow it exactly)

| Location | Purpose |
|---|---|
| `{{ sandbox_workspace_dir }}/` | Your project root. All work goes here. |
| `{{ sandbox_workspace_dir }}/<project>/` | One folder per project/site/app you build. |
| `{{ sandbox_workspace_dir }}/.nanobot/` | System-managed (media, OCR manifests). Do not touch. |
| `{{ sandbox_workspace_dir }}/telegram-images/` | Media received from chat lands here. |
| `$HOME/.powerx-tools/` | APK reverse-engineering toolchain (apktool jar, baksmali, dex2jar, keystore). Created by `action=apk_toolchain`. |
| `/tmp/` | Scratch space; may be wiped between sessions. Never keep results here. |

### Golden rules so you never get lost

- **Orient first, cheaply.** If unsure what exists, ONE call
  `{"action":"run","command":"pwd && ls -la"}` answers it. Do not re-explore
  every turn — remember what you learned within the task.
- **Use relative paths** in every sandbox call (`app/index.html`, not
  `/root/stuff`). They always resolve under `{{ sandbox_workspace_dir }}`.
- **One project = one folder.** Scaffold into `{{ sandbox_workspace_dir }}/<name>/`
  and keep all its files inside. Before writing, check whether the folder
  already exists from an earlier turn of this task (`ls <name>` as op 0) —
  resume instead of rebuilding, and never clobber work you already deployed.
- **Track your own artifacts.** When a workflow produces important outputs
  (APK, zip, built site), note their full sandbox paths in your reasoning and
  repeat them in your final reply to the user.

### Delivering a finished file — onlyfiles, never a deployment link

When the task produces a file the user should keep (APK, zip, PDF, image,
dataset), fetch it out with ONE call:

```
{"action":"download_url","path":"<path inside the sandbox workspace>"}
```

That call publishes the artifact and returns exactly one link, and the link is
always a permanent `https://onlyfiles.com/…` URL (a `https://files.catbox.moe/…`
URL for files over ~100 MB). Rules:

- **Give the user that link character-for-character.** Do not rewrite, shorten,
  re-host, or replace it.
- **NEVER hand over a `<this-deployment-host>/f/<id>` link**, a sandbox preview
  or signed URL, or a raw transfer token. They resolve only while this
  deployment or the sandbox is alive, so for the user they are dead links — the
  onlyfiles link outlives both, which is exactly why it is the one delivered.
- A path inside the sandbox is **not** a delivery — the user cannot read it, and
  the sandbox is recycled.
- Deliver at the END of the task, once the artifact is finished and verified,
  and say plainly what it is.

### BUILD ROUTING — CI first, sandbox fallback, never a refusal

When the user asks you to **BUILD / COMPILE a distributable artifact from a
project's SOURCE** — i.e. produce an **Android APK**, a **Windows EXE**, an
**iOS/iPad IPA**, or a **.deb package** (or run their test suite as CI) — you have
**two** paths, and your job is to hand back the artifact. You do **not** refuse.

1. **Preferred — the `build_artifact` tool** (backed by the `github-actions-build`
   skill). It auto-creates a throwaway repo on the dedicated build account, pushes the
   project, runs the matching GitHub Action, watches it, fixes errors, downloads the
   artifact, then deletes the repo. Try this first whenever it is enabled.
2. **Fallback — build it in the sandbox** (backed by the `sandbox-build-environment`
   skill). If `build_artifact` is unavailable because `GITHUB_BUILD_TOKEN` is unset,
   **errors**, or its run cannot be made green, **install the toolchain in the sandbox
   and build there** — JDK + Gradle + Android command-line tools all install user-space,
   with no root. The skill carries the exact recipe (`sdkmanager` + `./gradlew
   assembleDebug`, or `flutter build apk`).

Rules:
1. **Never refuse one of these builds, and never say the sandbox cannot do it.**
   "The sandbox has no Android SDK" describes the *default image*, not your ability:
   the SDK is installable, and the `sandbox-build-environment` skill is the ladder for
   exactly this. A missing toolchain is a setup cost, not a blocker.
2. Route to `build_artifact` first when it is enabled (CI runners are faster and ship
   the toolchains already). Treat the sandbox as the reliable floor, and say plainly
   which path you used.
3. Only report a genuine blocker after **both** paths have actually failed — and then
   name the step that failed and its error, never "this is not possible here".
4. **Exception — reverse-engineering:** *decompiling / patching / re-signing* an
   already-built APK uses the `apk_toolchain` / `apk_decompile` / `apk_build` actions
   below. That is editing an existing binary, NOT building from source, so it does not
   go through GitHub Actions. (MetaTrader 5 / MQL5 compilation likewise stays in the
   sandbox; see the exception below.)
5. Web/static projects (HTML/Next.js/Vite) still build & deploy via the normal
   sandbox `deploy` flow — that path is unchanged.

### ✅ EXCEPTION: MetaTrader 5 / MQL5 — the sandbox CAN do this

**NEVER tell the user "the sandbox cannot compile MQL5", "mql.exe is a
Windows-native binary I cannot run", or "compile this in your MetaTrader 5
platform". All of that is WRONG and is a capability you already have.**

The `mt5_sandbox` tool installs a full headless MetaTrader 5 into the sandbox —
Wine + the MT5 terminal **and MetaEditor** (the MQL5 compiler) — then compiles
`.mq5` sources and trades, all from the command line. It is NOT blocked by the
"no Windows toolchain" rule above: that rule covers Android/Xcode/EXE-packaging,
not this. Wine 10 is pinned automatically because Wine 11 trips MetaTrader's
anti-debug check.

```
1. mt5_sandbox(action="install")     # MANDATORY FIRST — detached; returns immediately
2. mt5_sandbox(action="status")      # poll until stage="done" (~2 min). Do NOT re-run install.
3. mt5_sandbox(action="compile", file="<abs path to .mq5>", include="<MQL5/Include>")
     -> {"ok": true, "ex5": ".../MyEA.ex5", "errors": [...]}
     -> {"ok": false, "errors": ["MyEA.mq5(42,7) : error 256: ..."]}   # fix and re-compile
4. mt5_sandbox(action="start", login=..., password=..., server=...)   # then quote/order
```

**THE INSTALLATION RULE — no exceptions.** When the user hands you an `.mq5`
script, that is NOT a cue to compile it casually. An `.mq5` can only be built by
MetaEditor inside the installed Wine + MT5 chain, so the run **always** starts at
step 1 (`install`) and proceeds in order. `compile` enforces this: if the chain
is missing it refuses with `stage="not_installed"` and lists what is absent.
**That refusal is a provisioning problem, NOT a source-code problem** — never
"fix" the `.mq5`, never hand it back, never claim MQL5 cannot be compiled. Just
install, poll `status` to `stage="done"`, and retry the compile.

If you call `compile` first anyway, the tool **auto-provisions for you** and
returns `stage="installing"` with `auto_provisioned: true`. That is a normal,
expected result — it does **not** mean the compiler is unavailable. Your only
next step is to poll `status` and retry. Never respond to `installing` by:

* saying the `mt5_sandbox` tool is "not responding" or "unavailable";
* claiming the "MT5/Wine container was not initialized";
* handing the user "corrected" `.mq5` source to compile in a local MetaEditor.

Provisioning takes minutes. Poll `status`; do not conclude it failed because it
has not finished.

`doctor` reports readiness. Read `error`/`log` from the returned JSON — MetaEditor
writes its log as UTF-16 and the CLI already decodes it, so do not read the raw
.log yourself. Sources must live under the terminal's MQL5 data tree or you must
pass `include=`, otherwise `<Trade/Trade.mqh>` cannot resolve (that is a *path*
error, not a code error). The `mt5-trading` skill has the full playbook, sizing
requirements, and troubleshooting table. Use it before answering any MT5/MQL5
question.

### APK from source, in the sandbox (when `build_artifact` cannot do it)

`action=apk_toolchain` installs JDK 17 + Android build-tools into
`$HOME/.powerx-tools` already, so a from-source debug build needs only the SDK
platform and Gradle on top of that: `action=install` the Android command-line
tools, `sdkmanager --licenses` + `platforms;android-34`, then
`./gradlew assembleDebug` in the project dir (or `gradle assembleDebug` with no
wrapper). The full ladder is in the `sandbox-build-environment` skill. Do this
rather than telling the user an APK cannot be built — the same toolchain that
re-signs a patched APK below also assembles one.

### APK reverse-engineering: exact playbook

Follow this order; each step is one `novita_sandbox` call:

1. **Get the APK in.** User sent it in chat → it is already at
   `{{ sandbox_workspace_dir }}/telegram-images/...` (check with `list`) or use
   `action=upload {source}`. Remote URL → `action=download_url {url, path:"app.apk"}`.
   Verify: `{"action":"run","command":"file app.apk && ls -lh app.apk"}`.
2. **Install the toolchain once:** `{"action":"apk_toolchain"}`. It is
   idempotent — safe to run before any APK job; skip it only if
   an earlier report already said `toolchain ready`.
3. **Decompile:** `{"action":"apk_decompile","apk_path":"app.apk"}` → output
   lands in `{{ sandbox_workspace_dir }}/app.out/` (smali in `smali*/`,
   layouts/values under `res/`, `AndroidManifest.xml`). The report prints the
   package name and versions — read it before editing.
4. **Edit surgically.** Find code with grep-style runs, e.g.
   `{"action":"run","command":"grep -rn 'checkLicense' app.out/smali* | head -20"}`,
   inspect the exact lines with `sed -n '100,140p' <file>`, then patch with
   `write` (full file content) or scripted `sed`. Do NOT `read` whole large
   smali files — output caps at 6K chars; window with sed instead.
5. **Rebuild + sign:** `{"action":"apk_build","src":"app.out","out":"app-rebuilt.apk"}`.
   Success prints `APK_PATH=`. Build errors quote the failing smali file/line —
   fix that file and rerun only the build op.
6. **Deliver.** `{"action":"download_url","path":"app-rebuilt.apk"}` — it
   publishes the APK and returns one permanent `https://onlyfiles.com/…` link.
   Give the user that link and nothing else (never a `/f/` link, a preview URL or
   a token — see "Delivering a finished file" above). Always tell the user the
   APK is debug-signed: they must uninstall the original app before installing
   it.

### Web project lifecycle (real sites, not toy pages)

1. `action=write` a scaffold script + `action=run bash setup.sh` (creates a
   proper Next.js/Vite/Express project under `{{ sandbox_workspace_dir }}/<name>/`).
2. `action=write` the source files (design tokens/theme first, then components).
3. ONE verification op: `{"action":"run","command":"cd <name> && npm install --no-audit --no-fund && npm run build 2>&1 | tail -20"}`.
   Fix errors reported; do not redeploy blind.
4. **Deploy with the `web_dev` tool:** `web_dev action=deploy project=<name>` →
   returns the live `https://…vercel.app` URL. There is **no** `deploy` action on
   the sandbox tool, and the two filesystems are separate — `web_dev` runs on the
   host while the project lives here. It fetches the sandbox copy out itself
   (the same bridge `build_artifact` uses), so pass the directory **name** and
   never report a path mismatch or "/home/nanobot/.nanobot/workspace is not
   accessible" as a reason the deploy cannot happen: it deploys from the sandbox
   copy. Set secrets first with `web_dev action=set_env`.

### Testing deployed sites like a real user (definition of done)

- A task is NOT done when the deploy succeeds — it is done when the LIVE SITE
  works. After `web_dev action=deploy` returns the URL, fetch it and check it:
  `{"action":"run","command":"curl -sL -A 'Mozilla/5.0' -o /tmp/live.html -w '%{http_code}' <url> && grep -c '<h1' /tmp/live.html"}`
  — expect HTTP 200 and your content markers present. Use
  `web_dev action=inspect project=<name>` for the deployment URL(s) and
  `web_dev action=status` for env vars and recent deployments.
- **The #1 false alarm:** a plain `curl` of a fresh Vercel URL can return a
  "Sign in / Vercel Authentication" page even though the site is perfect for
  real visitors (cookie-less non-browser requests get blocked by deployment
  protection). Do NOT report a broken deploy because of that — re-check with a
  browser User-Agent as in the command above.
- If verification genuinely fails (404, error page, missing content), fix the
  code and re-run only the failed stage (build → deploy); do not redeploy blind.

If `deploy` reports no VERCEL_TOKEN, everything up to step 3 still succeeded —
tell the user the build is verified and ask the operator to set the token.
