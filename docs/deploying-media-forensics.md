# Deploying the media-forensics sandbox relay

`media_forensics` answers "is this receipt edited, and when was it taken?". The
pixel and OCR work runs **inside the user's execution sandbox**, not on the
application host. This document covers how that relay provisions, what it needs
from the network, and how to read its failures.

For what the analysis can and cannot prove, see the `media-forensics` skill —
this file is about plumbing.

## What provisioning needs

The host ships its own forensics code into the sandbox. It does **not** download
anything at provision time.

Concretely, the relay packs the runner (`scripts/forensics_sandbox_runner.py`)
and the five package modules the runner imports into one tar, base64s it, writes
it through the sandbox `write` action in chunks, and unpacks it in the box after
verifying a SHA-256 digest. The only remaining network use inside the sandbox is:

| Needs egress to | For | Required? |
|---|---|---|
| PyPI | `numpy`, `pillow` | Yes, unless the image already has them |
| The distro mirror | `tesseract-ocr` | **No** — optional |
| GitHub | ~~runner + package~~ | **No longer used** |

A box with no GitHub access, no `curl`, or an egress proxy that does not pass
`raw.githubusercontent.com` can now run a full analysis. That is the property this
page exists to record, because the previous design did not have it.

> **History.** Provisioning used to resolve `main` through `api.github.com` and
> curl every file from `raw.githubusercontent.com`, on every box, on every
> provision. Sandboxes and VPSes that block GitHub failed that fetch, the
> bootstrap's `|| true` hid the failure, and the analysis then died on an
> `ImportError` — which surfaced to users as "media forensics never reaches the
> sandbox", identically on every backend. The version-pinning and cache-busting
> work that preceded this (commit-pinned URLs, a `FORENSICS_VERSION` grep) was
> real effort correctly applied to the wrong problem: the dependency itself.
> Shipping the payload from the host removes the failure class instead of
> reporting it better.

## Why the payload is shipped, not fetched

The runner must execute *the same analysis code the host would run*, so the
host's own installed package is the only correct source. Downloading from a
branch introduces a version-skew failure that cannot be tested for at runtime: a
runner from one commit importing a package from another.

Two consequences worth knowing:

- **A pinned release tag is not required for correctness anymore.** The sandbox
  runs the host's code, so deploying an older nanobot gets the older analysis in
  the box too, consistently.
- **The payload is built once per process** and cached (`_provision_payload`). The
  tar is ~150 KB uncompressed.

## Version rollover

`FORENSICS_VERSION` in `nanobot/agent/tools/forensics_sandbox.py` must equal the
value in `scripts/forensics_sandbox_runner.py`. It also names the ready-marker
file inside the sandbox:

```
.forensics/ready-2026-09-27.2
```

**Bumping the version is therefore the release mechanism.** Every existing box
fails its marker check and re-provisions on the next analysis; a stale runner
cannot survive a release. Bump it whenever the runner's contract or the package's
analysis changes.

The runner and package are excluded from the per-analysis transfer, and
`benchmark.py` is not shipped at all: nothing in the analysis path imports it, it
is the largest file in the package, and every analysis pays for a round trip per
100 KB uploaded.

## The workspace-root problem (still here, still per-backend)

The sandbox exposes a `write` action that resolves paths against the backend's own
workspace root, and a `run` action that starts a shell in a directory nobody can
predict. Those two must agree or the payload is written to one place and unpacked
from another.

Measured roots: `/root` on a stock Novita box (which has **no `/workspace` at
all**), `/home/user` Runloop, `/home/daytona` Daytona, `/home/tenki` Tenki,
`/vercel/sandbox` Vercel, `/workspace/home` Upstash, and `workspace_dir` on a VPS.

The relay's answer is to name no root at all. Every path it passes to `write` is
**relative**, and every shell command opens with a prelude that finds the root by
looking for a breadcrumb file (`.forensics_root`) that only `write` could have put
there, then `cd`s into it. The root is *derived from* the write action rather than
assumed about it, so the two halves are structurally unable to disagree. A new
backend needs no change here.

When the prelude cannot find a root it prints `FORENSICS_ROOT_UNRESOLVED` and
exits non-zero, which is why the failure below is named rather than mysterious.

## Reading a failure

| Message | Meaning | Do this |
|---|---|---|
| `the sandbox could not be provisioned for forensics (could not locate the sandbox workspace root)` | `write` and `run` disagree about the root, or the breadcrumb is gone | Retry; on a new backend, check that `write` accepts relative paths |
| `... (the sandbox has no forensics payload written to it)` | The payload chunks never landed | Retry; check `write` succeeded |
| `... (the forensics payload failed its checksum in the sandbox)` | A chunk was truncated | Retry — the transfer is rebuilt each provision |
| `... (the forensics payload would not unpack in the sandbox)` | No `tar`, or a read-only root | Check the image |
| `... (DEPS_MISSING / deps: missing)` | numpy/Pillow absent and PyPI unreachable | Pre-install them in the sandbox template |
| `pixels read in the sandbox (<backend>, peak N MB, Ns)` | **Success** — the report footer naming where the pixels were read | nothing |

`sandbox="auto"` (the default) falls back to analysing on the host whenever the
sandbox cannot serve the request, and the footer says so. `sandbox="require"`
refuses instead — use it when host CPU is the thing you are protecting.

## Verifying a deployment

A relay that provisions and a relay that *analyses* are different claims. This
drives the real tool against a real Novita box:

```bash
export NOVITA_API_KEY=...      # sandbox credentials come from the environment
python - <<'PY'
import asyncio, os
from pathlib import Path
from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool
from nanobot.agent.tools.forensics_sandbox import ForensicsRelay
from nanobot.forensics.benchmark import render_receipt, _save

tmp = Path("/tmp/fx-check"); tmp.mkdir(parents=True, exist_ok=True)
r = render_receipt(42); img = tmp / "receipt.jpg"; _save(r.image, img, 88)

async def main():
    relay = ForensicsRelay(NovitaSandboxTool())
    print(await relay.provision())            # {'ok': True, 'tesseract': True, ...}
    p = await relay.analyse([(img.name, img)], document=True,
                            expected_amount=str(r.amount), timeout=600)
    f = p["files"][img.name]
    print("version", p["version"], "ocr lines",
          len(f["document"]["text_lines"]),
          "recon", f["document"]["reconciliation"]["matches"])
asyncio.run(main())
PY
```

Expected on a healthy box: `ok: True`, a `tesseract` answer, the payload version
echoing `FORENSICS_VERSION`, at least one OCR line, and a reconciliation match
against the amount you passed.

The regression tests are the faster signal and need no credentials:

```bash
pytest tests/tools/test_media_forensics_sandbox.py tests/test_media_forensics.py \
       tests/test_forensics_tamper.py
```

`test_provisioning_needs_no_network_egress_from_the_sandbox` asserts the bootstrap
contains no URL, no `curl`, and no `wget`. If a future change reintroduces a
download, that test fails and this page needs rewriting again.

## Measured behaviour

Re-run the harness rather than trusting any number here:

```bash
python -m nanobot.forensics.benchmark --count 12 --seed 7 --documents
```

On the current tree: **0 of 12 clean receipts** left the clean band, and **12 of
12** amount-replacement forgeries were caught per class (`replaced_amount`,
`_same_quality`, `_low_quality`) by arithmetic reconciliation. The pixel layer
still only separates clone-stamping; a replaced total is invisible to it. That is
the honest summary and the skill says so at length.
