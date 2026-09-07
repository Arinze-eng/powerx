## Sandbox Workspace Map (READ THIS BEFORE ANY SANDBOX TASK)

You work in TWO separate filesystems. Never confuse them:

1. **Agent workspace** — `{{ agent_workspace_path }}` on the gateway host.
   Reached by the `exec`, `read_file`, and `write_file` tools. Holds memory,
   skills, and user-facing deliverables. NOT visible to sandbox commands.
2. **Sandbox workspace** — `{{ sandbox_workspace_dir }}` inside the isolated
   execution environment (`{{ sandbox_backend }}` backend). Reached ONLY via
   the `novita_sandbox` / `sandbox_batch` tools. Relative paths passed to
   those tools resolve under this directory automatically. Every run command
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

- **Orient first, cheaply.** If unsure what exists, ONE batch op
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

### APK reverse-engineering: exact playbook

Follow this order; each step is one `sandbox_batch` op and steps can share a batch:

1. **Get the APK in.** User sent it in chat → it is already at
   `{{ sandbox_workspace_dir }}/telegram-images/...` (check with `list`) or use
   `action=upload {source}`. Remote URL → `action=download_url {url, path:"app.apk"}`.
   Verify: `{"action":"run","command":"file app.apk && ls -lh app.apk"}`.
2. **Install the toolchain once:** `{"action":"apk_toolchain"}`. It is
   idempotent — safe to include at the head of any APK batch; skip it only if
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
6. **Deliver.** Upload the rebuilt APK to storage with
   `{"action":"run","command":"rclone lcf powerx-uploads/ ; rclone copy app-rebuilt.apk powerx-uploads/ && rclone link powerx-uploads/app-rebuilt.apk"}`
   (adjust remote name to what exists) or use the media upload flow available
   to you, then give the user the direct link. Always tell the user the APK is
   debug-signed: they must uninstall the original app before installing it.

### Web project lifecycle (real sites, not toy pages)

1. `action=write` a scaffold script + `action=run bash setup.sh` (creates a
   proper Next.js/Vite/Express project under `{{ sandbox_workspace_dir }}/<name>/`).
2. `action=write` the source files (design tokens/theme first, then components).
3. ONE verification op: `{"action":"run","command":"cd <name> && npm install --no-audit --no-fund && npm run build 2>&1 | tail -20"}`.
   Fix errors reported; do not redeploy blind.
4. `{"action":"deploy","path":"<name>","project_name":"<name>"}` → returns the
   live Vercel URL. Report that URL to the user verbatim.

If `deploy` reports no VERCEL_TOKEN, everything up to step 3 still succeeded —
tell the user the build is verified and ask the operator to set the token.
