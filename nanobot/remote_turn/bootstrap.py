"""Provision the sandbox on demand, so a turn can actually run there.

WHY THIS EXISTS
---------------
``NANOBOT_TURN_HOST=sandbox`` moves a whole turn -- the model calls and the tools
-- into the execution sandbox, leaving the host as a relay. The dispatcher runs
the worker as ``python3 -m nanobot.remote_turn.worker <request.json>``, and that
command needs the ``nanobot`` package to exist *on the box*. It did not: the
sandbox images ship a bare Linux userland, and baking this project's dependency
tree into every sandbox template is precisely the memory cost the feature exists
to remove. The result was that the switch moved no turns at all, and every one
of them was served by the host anyway.

So the package is provisioned *inside the sandbox, by the turn that needs it* --
the same shape ``media`` and ``mt5_sandbox`` already use for their toolchains,
and for the same reason (see the NOTE in the Dockerfile: the toolchain belongs in
the sandbox that owns the work, not in the gateway image).

WHAT TRAVELS
------------
One shell snippet. It fetches ``scripts/install_nanobot_sandbox.sh`` from the
commit-pinned GitHub URL and runs it, then runs the worker. The installer is
idempotent and fast-exits when the box is already provisioned at the current
``main``, so the steady-state cost is one API call and one small download.

The pin matters. The sandbox's egress path caches ``raw.githubusercontent.com``
by *path*, so a branch URL has been measured serving a revision several pushes
old (``media.py`` documents the same trap). Resolving ``main`` to a SHA through
the API and downloading that exact URL is what makes the code that runs in the
sandbox the code that was pushed.

SKIPPING IT
-----------
On a long-lived backend where an operator provisions the box once (VPS),
``NANOBOT_TURN_BOOTSTRAP=0`` drops the provisioning steps and runs the worker
alone. That is the only reason the switch exists: on a TTL'd sandbox the box is
recycled, so re-provisioning has to be the default.
"""

from __future__ import annotations

import os

#: Where the sandbox keeps the provisioned package. A literal ``$HOME`` so the
#: remote shell expands it -- the host does not know the sandbox user's home.
SANDBOX_HOME = "$HOME/.nanobot"

#: The extracted source tree. Put on ``PYTHONPATH`` for the worker so the pinned
#: commit's code shadows the copy installed in the venv, the same way the gateway
#: image pins ``PYTHONPATH=/app`` over its own site-packages copy.
SOURCE_DIR = f"{SANDBOX_HOME}/nanobot-src"

#: The interpreter the package is installed into.
VENV_DIR = f"{SANDBOX_HOME}/venv"

#: The installer itself.
INSTALLER_PATH = f"{SANDBOX_HOME}/bin/install_nanobot_sandbox.sh"

#: owner/repo the installer is fetched from.
REPO = os.getenv("NANOBOT_SCRIPT_REPO", "Arinze-eng/powerx")

#: Last-resort raw base, used only when the GitHub API cannot resolve ``main``.
RAW_BASE = os.getenv(
    "NANOBOT_SCRIPT_RAW_BASE",
    f"https://raw.githubusercontent.com/{REPO}/main/scripts",
)

#: Set to a falsy value to run the worker without re-provisioning the box.
BOOTSTRAP_ENV = "NANOBOT_TURN_BOOTSTRAP"

#: How long provisioning may take. A cold install of this dependency tree is
#: minutes, and the backends cap a single command at 900 s, so this sits at that
#: ceiling: a longer budget would be silently truncated by the backend instead.
BOOTSTRAP_TIMEOUT_S = 900


def bootstrap_enabled() -> bool:
    """Whether a dispatch should provision the sandbox before running the worker.

    Read live rather than at import, so an operator can turn it off (or back on)
    without a restart -- the same contract as the other PowerX switches.
    """
    raw = os.environ.get(BOOTSTRAP_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "skip"}


def provision_command() -> str:
    """The shell snippet that makes the sandbox able to run a worker.

    Fetches the installer from a commit-pinned URL and runs it. The installer
    decides whether there is anything to do, so this can run on every turn
    without the host tracking the box's state.
    """
    return f"""mkdir -p {SANDBOX_HOME}/bin
_inst={INSTALLER_PATH}
_sha=$(curl -fsSL --retry 2 'https://api.github.com/repos/{REPO}/commits/main' 2>/dev/null \\
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_bases="{RAW_BASE}"
if [ -n "$_sha" ]; then _bases="https://raw.githubusercontent.com/{REPO}/$_sha/scripts $_bases"; fi
_ok=''
for _b in $_bases; do
  if curl -fsSL --retry 2 "$_b/install_nanobot_sandbox.sh" -o "$_inst" 2>/dev/null \\
     && sh -n "$_inst" 2>/dev/null; then _ok=1; break; fi
done
if [ -z "$_ok" ]; then
  echo "remote turn: could not fetch the sandbox installer" >&2
  exit 1
fi
if command -v bash >/dev/null 2>&1; then bash "$_inst" 1>&2 || exit 1
else sh "$_inst" 1>&2 || exit 1
fi"""


def worker_command(request_path: str) -> str:
    """The command that runs one turn, on the interpreter that has the package.

    A single shell command on purpose: it is what lets :func:`dispatch_command`
    chain provisioning in front of it with ``&&``, so a box that could not be
    provisioned never reaches a worker that is not there.

    The venv is preferred because that is where the package is installed;
    ``python3`` stays as the fallback so a box an operator provisioned by hand
    still runs. ``PYTHONPATH`` points at the extracted source tree so the commit
    the installer just fetched is the commit that runs, rather than whatever the
    venv happened to be built from.
    """
    interpreter = f'"$([ -x {VENV_DIR}/bin/python ] && echo {VENV_DIR}/bin/python || echo python3)"'
    return (
        f"PYTHONPATH={SOURCE_DIR} {interpreter} "
        f"-m nanobot.remote_turn.worker {request_path}"
    )


def dispatch_command(request_path: str) -> str:
    """The whole dispatch as one shell line: provision, then run the worker.

    Provisioning is **best-effort**, and deliberately not chained with ``&&``: it
    is a *refresh*, not a precondition. A sandbox an earlier turn already
    provisioned must keep working when GitHub is briefly unreachable, and turning
    a failed refresh into a failed turn would take a healthy sandbox offline.

    The dispatcher runs these as two separate commands rather than using this
    line, for one reason: timeouts. Provisioning is allowed the backends' full
    ``BOOTSTRAP_TIMEOUT_S``, while the turn keeps its own budget -- one shared
    timeout would let a slow install eat the turn's time. This function is the
    equivalent single command, with identical semantics, for callers that have
    only one shot at running something.
    """
    worker = worker_command(request_path)
    if not bootstrap_enabled():
        return worker
    # The subshell is load-bearing. ``provision_command`` ends in ``exit 1`` when
    # it cannot fetch the installer, and an unwrapped ``exit`` would terminate
    # this whole shell before the worker ran -- turning a failed *refresh* into a
    # failed *turn*, which is the one thing provisioning must never cause. The
    # subshell also keeps the snippet's helper variables out of the worker's
    # environment.
    return f"( {provision_command()} ); {worker}"


__all__ = [
    "BOOTSTRAP_ENV",
    "BOOTSTRAP_TIMEOUT_S",
    "INSTALLER_PATH",
    "REPO",
    "SANDBOX_HOME",
    "SOURCE_DIR",
    "VENV_DIR",
    "bootstrap_enabled",
    "dispatch_command",
    "provision_command",
    "worker_command",
]
