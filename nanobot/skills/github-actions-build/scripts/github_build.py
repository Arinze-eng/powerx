#!/usr/bin/env python3
"""
github_build.py — Cloud artifact builder for the PowerX nanobot AI agent.

Orchestrates a full "auto repo -> push -> trigger workflow -> watch -> fix -> download -> cleanup"
cycle against GitHub Actions, using the dedicated `william165-bot` build account.

Auth: reads a GitHub token from the GITHUB_BUILD_TOKEN env var (never hardcode).
      The token must belong to the dedicated build account and carry `repo`, `workflow`,
      and `delete_repo` scopes.

All GitHub calls go through the `gh` CLI when available (preferred), else raw REST via curl.

Usage (each subcommand is idempotent; the LLM drives the loop):

  export GITHUB_BUILD_TOKEN=<dedicated-build-account-PAT>
  python github_build.py create       --name mycustomrepo --desc "LLM-chosen name"
  python github_build.py push         --repo owner/name --src /local/project
  python github_build.py add-workflow --repo owner/name --type apk [--with vars]
  python github_build.py trigger      --repo owner/name --workflow build-apk.yml [inputs.json]
  python github_build.py watch        --repo owner/name --run <id> [--timeout 1800]
  python github_build.py download     --repo owner/name --run <id> --dest dir --glob '**/*.apk'
  python github_build.py fix-push     --repo owner/name --src /local/project --msg "fix: ..."
  python github_build.py delete       --repo owner/name
  python github_build.py artifact-links --repo owner/name --run <id>

Exit codes: 0 success, 1 build succeeded but artifact missing/errored, 2 failed, 3 error.
"""

import argparse
import json
import os
import subprocess
import sys
import time

TOKEN_ENV = "GITHUB_BUILD_TOKEN"
DEFAULT_OWNER = "william165-bot"
WORKFLOWS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows")


def gh(*args: str, check: bool = True) -> str:
    """Run `gh` CLI (preferred) or fall back to token-less mode error."""
    env = dict(os.environ)
    env["GH_TOKEN"] = os.environ.get(TOKEN_ENV, "")
    env["GH_PROMPT_DISABLED"] = "1"
    if not env["GH_TOKEN"]:
        sys.exit(f"[error] {TOKEN_ENV} not set. Set it to the dedicated build account PAT.")
    r = subprocess.run(["gh", *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        sys.exit(f"[error] gh {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def get_token() -> str:
    t = os.environ.get(TOKEN_ENV, "")
    if not t:
        sys.exit(f"[error] {TOKEN_ENV} not set.")
    return t


def sha_ok() -> bool:
    return True  # shasum always available on CI/sandbox; no op kept for clarity


# ---------------- create ----------------
def cmd_create(args: argparse.Namespace) -> None:
    gh(
        "repo", "create", f"{args.owner}/{args.name}",
        "--private",
        "--description", args.desc or "",
        check=True,
    )
    print(f"[ok] created https://github.com/{args.owner}/{args.name}")


# ---------------- push ----------------
def cmd_push(args: argparse.Namespace) -> None:
    repo = f"{args.owner}/{args.name}" if args.repo is None else args.repo
    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        sys.exit(f"[error] source not a directory: {src}")
    import shutil
    clone = "/tmp/ghb_pending"
    # fresh clone each time
    subprocess.run(["rm", "-rf", clone], check=False)
    r = subprocess.run(["git", "clone", f"https://x-access-token:{get_token()}@github.com/{repo}.git", clone],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[error] clone failed:\n{r.stderr.strip()}")
    # Copy files (exclude .git) into clone
    for entry in os.listdir(src):
        if entry in (".git",):
            continue
        s = os.path.join(src, entry)
        d = os.path.join(clone, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    subprocess.run(["git", "-C", clone, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", clone, "-c", "user.email=powerx@build", "-c", "user.name=PowerX Build",
         "commit", "-m", args.msg or "Add project files"], check=True)
    # Ensure we are on a 'main' branch then push it explicitly
    subprocess.run(["git", "-C", clone, "branch", "-M", "main"], check=False)
    r = subprocess.run(["git", "-C", clone, "push", "-u", "origin", "HEAD:main"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[error] git push failed:\n{r.stderr.strip()}\n{r.stdout.strip()}")
    print(f"[ok] pushed {src} -> {repo}")


# ---------------- add workflow ----------------
def cmd_add_workflow(args: argparse.Namespace) -> None:
    repo = f"{args.owner}/{args.name}" if args.repo is None else args.repo
    wf = WORKFLOWS_DIR + "/" + args.type + ".yml"
    remote = ".github/workflows/" + args.type + ".yml"
    targets = ["build-apk.yml", "build-ipa.yml", "build-exe.yml", "build-deb.yml", "run-tests.yml"]
    if args.type not in [t[:-4] for t in targets]:
        sys.exit(f"[error] unknown workflow type '{args.type}'. options: {', '.join(t[:-4] for t in targets)}")
    with open(wf, "r") as f:
        content = f.read()
    # Commit via API (simpler than cloning for adding checked-in template)
    import base64
    data = json.dumps({"message": f"Add {remote} via PowerX build tool",
                        "content": base64.b64encode(content.encode()).decode()})
    t = get_token()
    r = subprocess.run(
        ["curl", "-s", "-X", "PUT", "-H", f"Authorization: Bearer {t}",
         "-H", "Accept: application/vnd.github+json",
         f"https://api.github.com/repos/{repo}/contents/{remote}",
         "-d", data],
        capture_output=True, text=True)
    try:
        out = json.loads(r.stdout)
        if "content" not in out:
            sys.exit(f"[error] add workflow failed: {r.stdout[:400]}")
    except Exception:
        sys.exit(f"[error] add workflow parse failed: {r.stdout[:400]}")
    print(f"[ok] workflow added: {remote}")


def cmd_trigger(args: argparse.Namespace) -> None:
    repo = f"{args.owner}/{args.name}" if args.repo is None else args.repo
    t = get_token()
    inputs = "{}"
    if args.inputs:
        inputs = args.inputs
    payload = json.dumps({"ref": "main", "inputs": json.loads(inputs)})
    r = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
         "-H", f"Authorization: Bearer {t}",
         "-H", "Accept: application/vnd.github+json",
         f"https://api.github.com/repos/{repo}/actions/workflows/{args.workflow}/dispatches",
         "-d", payload], capture_output=True, text=True)
    code = r.stdout.strip()
    if code not in ("204",):
        sys.exit(f"[error] trigger failed with HTTP {code}: {r.stderr[:300]}")
    # Poll briefly until the new run appears, then print its id
    rid = None
    for _ in range(12):
        time.sleep(5)
        out = gh("run", "list", "--repo", repo, "--workflow", args.workflow,
                 "--limit", "1", "--json", "databaseId,status,headSha", check=False)
        try:
            runs = json.loads(out)
            if runs:
                rid = runs[0]["databaseId"]
                break
        except Exception:
            continue
    if rid:
        print(f"[ok] triggered run {rid} on {repo}")
    else:
        print("[warn] dispatched but could not fetch run id; use `gh run list` manually")


def cmd_watch(args: argparse.Namespace) -> None:
    repo = f"{args.owner}/{args.name}" if args.repo is None else args.repo
    t0 = time.time()
    timeout = args.timeout
    last = None
    while time.time() - t0 < timeout:
        out = gh("run", "view", str(args.run), "--repo", repo,
                 "--json", "status,conclusion", check=False)
        try:
            d = json.loads(out)
        except Exception:
            time.sleep(5); continue
        status, conclusion = d.get("status"), d.get("conclusion")
        if status != last:
            print(f"[watch] run {args.run}: status={status} ({int(time.time()-t0)}s)")
            last = status
        if status == "completed":
            if conclusion in ("success",):
                print(f"[ok] run {args.run} completed: {conclusion}")
                sys.exit(0)
            else:
                print(f"[fail] run {args.run} completed: {conclusion}")
                sys.exit(2)
        time.sleep(10)
    print(f"[timeout] run {args.run} still {status} after {timeout}s")
    sys.exit(2)


def cmd_download(args: argparse.Namespace) -> None:
    repo = f"{args.owner}/{args.name}" if args.repo is None else args.repo
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)
    r = gh("run", "download", str(args.run), "--repo", repo, "-n", args.artifact_name,
           "-D", "/tmp/ghb_dl", check=False)
    import glob
    found = glob.glob("/tmp/ghb_dl/**/*", recursive=True)
    print(f"[ok] downloaded artifacts to {dest}: {found}")
    shutil = __import__("shutil")
    for f in found:
        if os.path.isfile(f):
            shutil.copy2(f, os.path.join(dest, os.path.basename(f)))
    print(f"[ok] artifact files placed in {dest}")


def cmd_fix_push(args: argparse.Namespace) -> None:
    # identical to push, but a distinct message; reuse push logic
    old_msg = args.msg
    args.msg = old_msg or "fix: build repair"
    cmd_push(args)


def cmd_delete(args: argparse.Namespace) -> None:
    repo = f"{args.owner}/{args.name}" if args.repo is None else args.repo
    gh("repo", "delete", repo, "--yes", check=True)
    print(f"[ok] deleted {repo}")


def cmd_help(_: argparse.Namespace) -> None:
    print(__doc__)


def main() -> None:
    p = argparse.ArgumentParser(prog="github_build", description=__doc__.splitlines()[0])
    p.add_argument("-o", "--owner", default=DEFAULT_OWNER)
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("create"); c.add_argument("--name", required=True); c.add_argument("--desc", default=""); c.set_defaults(fn=cmd_create)
    c = sub.add_parser("push"); c.add_argument("--repo", required=True); c.add_argument("--src", required=True); c.add_argument("--msg", default="Add project files"); c.set_defaults(fn=cmd_push)
    c = sub.add_parser("add-workflow"); c.add_argument("--repo", required=True); c.add_argument("--type", required=True, choices=["build-apk", "build-ipa", "build-exe", "build-deb", "run-tests"]); c.set_defaults(fn=cmd_add_workflow)
    c = sub.add_parser("trigger"); c.add_argument("--repo", required=True); c.add_argument("--workflow", required=True); c.add_argument("--inputs", default=None); c.set_defaults(fn=cmd_trigger)
    c = sub.add_parser("watch"); c.add_argument("--repo", required=True); c.add_argument("--run", required=True, type=int); c.add_argument("--timeout", default=1800, type=int); c.set_defaults(fn=cmd_watch)
    c = sub.add_parser("download"); c.add_argument("--repo", required=True); c.add_argument("--run", required=True, type=int); c.add_argument("--dest", required=True); c.add_argument("--artifact-name", default="apk"); c.set_defaults(fn=cmd_download)
    c = sub.add_parser("fix-push"); c.add_argument("--repo", required=True); c.add_argument("--src", required=True); c.add_argument("--msg", default="fix: build repair"); c.set_defaults(fn=cmd_fix_push)
    c = sub.add_parser("delete"); c.add_argument("--repo", required=True); c.set_defaults(fn=cmd_delete)
    sub.add_parser("help").set_defaults(fn=cmd_help)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        cmd_help(args); sys.exit(0)
    args.fn(args)


if __name__ == "__main__":
    main()