# Artifact delivery — one permanent link, and it is an onlyfiles.com link

When a task produces a file the user should keep — an APK, an archive, a PDF, a
screenshot, a dataset — the deliverable is not a path inside an ephemeral
sandbox. It is **one link that still works tomorrow**, from a host that has
nothing to do with this deployment.

That link is the **onlyfiles.com page URL**.

```
sandbox(action="download_url", path="app-debug.apk")
   -> Download link (onlyfiles) - permanent, valid forever:
      https://onlyfiles.com/<id>/app-debug.apk
```

Hand the user that link, verbatim. Nothing else in the tool result is a link.

## Why onlyfiles, and what is *not* delivered

| Candidate link | Verdict |
|---|---|
| `https://onlyfiles.com/<id>/<name>` — the file page, uploaded with `expire=0` | **Delivered.** Permanent; needs nothing from us; mints a working download on every view. |
| `https://files.catbox.moe/<id>.<ext>` — for files over onlyfiles' ~100 MiB ceiling | **Delivered.** Permanent and direct. |
| `<your-deployment>/f/<id>` — the gateway's forced-download redirect | **Never delivered.** It resolves only while this deployment answers on that host, so a pasted link outlives it. |
| `https://onlyfiles.com/dl/<ts.nonce>/<id>/<name>` — a raw transfer token | **Never delivered.** Measured: each token carries a timestamp exactly **300 s** ahead of its mint time, and a fresh one is issued per page view. Useful for one immediate byte transfer, dead five minutes later. |
| A sandbox preview URL, a signed URL, a cloud-drive share | **Never delivered.** All of them expire or require an account the user does not have. |
| A path such as `/home/ubuntu/workspace/app-debug.apk` | **Not a delivery.** The user cannot read it and the sandbox is recycled. |

The rule exists because of what users actually reported: *"the link is not
working, use onlyfiles"* and *"llm should stop using
`https://<host>/f/<id>` … it should use onlyfiles"*. A link that depends on the
sandbox, on an expiring token, or on this deployment's own host is not a
deliverable.

## The documented onlyfiles contract

From <https://onlyfiles.com/api> (no expiry, no account, no key):

* `POST https://api.onlyfiles.com/v1/upload` — multipart, one `file` field and an
  `expire` field. `expire` is seconds (60–172800) or **`0` to keep the file
  forever**; the default is 86400. This deployment always sends `0`.
* The response carries
  `data.file.url.full` (`https://onlyfiles.com/<id>/<name>`) — the permanent
  link, and the one delivered — plus `data.file.metadata` (id, name, size).
* `GET https://api.onlyfiles.com/v1/file/{id}/info` — metadata / liveness probe;
  a missing file answers HTTP 404 with `status: false`.
* Limits: 100 MB per file, 500 files or 50 GB per hour, 5000 files or 100 GB per
  day. Above the size ceiling delivery falls back to catbox.moe.

Implementation lives in `nanobot/utils/onlyfiles.py` (the API and link policy)
and `nanobot/utils/file_share.py` (size routing and the delivery text), so every
sandbox backend — Novita, VPS, Upstash, Daytona, Runloop, Vercel, Tenki,
Freestyle — publishes the same way through `download_url`.

## What the model is told

Three places carry the rule, and a test guards each:

1. `nanobot/templates/agent/sandbox_workspace.md` — *"Delivering a finished file
   — onlyfiles, never a deployment link"*, read before any sandbox task.
2. The `novita_sandbox` tool description (the `download_url` action) and the
   `android_sandbox` tool description (`screenshot` / `pull`).
3. `nanobot/skills/sandbox-build-environment/SKILL.md` — *"Delivering the
   finished file"*, plus the delivery line in `video-editing` and
   `vulnerability-hunting`.

Delivered links are also remembered on the persistent disk
(`ArtifactLinkMemory`) so a repeat request replays the permanent onlyfiles URL
instead of rebuilding the artifact; the system prompt's *Durable Artifact Links*
section hands back the page URL, never a gateway link, even for records written
before this policy existed.

## Operating notes

* Nothing here needs configuration: no key, no bucket, no gateway host. If
  `POWERX_PUBLIC_URL` / `NANOBOT_API_PUBLIC_URL` are set, the gateway's
  `/f/<id>` redirect still works for internal callers, but delivery ignores it.
* Uploads are content-addressed in `UploadedUrlMemory`, so re-delivering the
  same bytes costs no second upload.
* A delivery that cannot reach a host surfaces as `Could not publish artifact
  link: …` — fix the transport rather than handing the user a sandbox path.
