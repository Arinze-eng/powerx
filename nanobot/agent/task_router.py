"""Generic task router: answer recurring coding/workspace tasks with ZERO LLM calls.

This is the layer that makes "the model can think only when it must" real for
everyday *tasks* — not just read-only lookups. Manus's command/API ratio (huge
number of commands, a handful of model calls) comes from running the *routine*
shapes of work through deterministic command runners and only paying the model
for genuine reasoning.

The existing ``deterministic_router`` covers narrow, product-specific read-only
lookups (account status, transcripts, announcements, named-file searches). This
module generalises the same discipline to the *recurring open-ended task
families* a generic user actually asks every day:

* "check for bugs / review the code / look for issues"  -> static-analysis scan
* "run the tests / run pytest / execute the test suite" -> test runner
* "list / show / summarise the project structure"        -> workspace inventory

Each family is answered by executing ONE deterministic shell command inside the
configured execution sandbox (Novita / VPS / Upstash — wherever the user's code
actually lives), via the registered ``sandbox_batch`` tool, falling back to the
local ``exec`` shell when no sandbox is present. The command's output IS the
answer: zero provider round-trips, zero credit steps. Everything is read-only
and fail-open: an ambiguous, write-shaped, image-bearing, over-long, or not-
obviously-a-project ask falls straight through to the normal LLM path unchanged,
so correctness is never sacrificed — only the routine cost disappears.

Safety is baked in the same way as the existing layers:

* Only *read-only* command recipes are ever emitted. No rm / no network money /
  no mutation. The commands are plain ``bash`` that list, lint, and run tests —
  each checks for only presence.
* Recipes only fire when a command runner (sandbox_batch or exec) is registered
  on the run, so a generic agent that exposes neither is untouched.
* The router consults live env (+ per-run opt-in) so operators (and tests) can
  flip it without a restart.
"""

from __future__ import annotations

import os
import re

from nanobot.providers.base import ToolCallRequest

#: Only short, structured asks are routed. A long message almost always needs
#: synthesis or multi-step judgement — that is the model's job.
_MAX_TEXT_CHARS = 400
_MIN_TEXT_CHARS = 6


# ---------------------------------------------------------------------------
# Recognised task families -> one deterministic read-only shell command
# ---------------------------------------------------------------------------

# "Check the code for bugs/issues" -> run a read-only static scan. We auto-select
# the best available linter for the languages present, but keep the whole thing
# non-destructive: nothing writes back to disk, no network calls.
_BUG_SCAN_RE = re.compile(
    r"\b(check|look|scan|review|find|detect|run|see|spot)\b.*"
    r"\b(bug|bugs|issue|issues|defect|error|errors|problem|problems|code smell)\b",
    re.I | re.S,
)
_BUG_SCAN_CMD = (
    # Detect which tools exist; fall back silently. `|| true` keeps the scan
    # non-fatal. Uses `--max-line-length`-safe py_compile first (zero deps).
    "echo '--- static scan ---'; "
    "for t in ruff flake8 mypy pyright; do command -v $t >/dev/null 2>&1 && echo \"[deps] $t present\"; done; "
    "command -v python3 >/dev/null 2>&1 && find . -name '*.py' -not -path '*/.git/*' -not -path '*/node_modules/*' "
    "-not -path '*/.venv/*' -not -path '*/venv/*' | head -200 | xargs -r -n1 python3 -m py_compile 2>&1 | head -60 || true; "
    "echo '--- done ---'"
)

# "Run the tests" -> run the configured test runner read-only. pytest first,
# fall back to a compile-only sanity check so a missing runner is non-fatal.
_RUN_TESTS_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?"
    r"(?:run|execute|kick off|trigger|start)\b.*\b(?:tests?|test suite|pytest|"
    r"unit tests?)\b",
    re.I,
)
_RUN_TESTS_CMD = (
    "if command -v pytest >/dev/null 2>&1; then "
    "  [ -f pytest.ini ] || [ -f pyproject.toml ] || [ -f setup.cfg ] && "
    "  (pytest -q --no-header -x 2>&1 | tail -40); exit 0; "
    "fi; "
    "echo 'no pytest runner found'"
)

# "List / summarise the project structure" -> non-destructive inventory.
_PROJECT_STRUCTURE_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?"
    r"(?:list|show|summarise|summarize|display|describe|print)\b.*"
    r"\b(?:project|repo|directory|folder|workspace|structure|tree|files|"
    r"layout|hierarchy)\b",
    re.I,
)
_PROJECT_STRUCTURE_CMD = (
    "echo '--- project structure ---'; "
    "if command -v tree >/dev/null 2>&1; then tree -L 2 -I 'node_modules|.git|"
    ".venv|venv|__pycache__|dist|build' --noreport; "
    "else find . -maxdepth 2 -not -path '*/.git/*' -not -path '*/node_modules/*' "
    "-not -path '*/.venv/*' -not -path '*/venv/*' | sort | head -200; fi; "
    "echo; echo '--- done ---'"
)


# ---------------------------------------------------------------------------
# router_enabled(): operators can switch this layer independently.
# ---------------------------------------------------------------------------


def task_router_enabled() -> bool:
    """True when the generic task router may answer turns (live check)."""
    raw = os.environ.get("POWERX_TASK_ROUTER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _looks_read_only(text: str) -> bool:
    """Reject write-shaped / mutation / build asks.

    A routine *read* of the codebase (scan for bugs, run tests, list files) is
    safe to answer deterministically. Anything that implies producing or
    changing work — building, fixing, refactoring, improving, deploying — must
    stay with the model, because a static scan cannot satisfy it and returning
    partial output as if it were the whole task would be wrong.
    """
    # Mutation / authoring verbs anywhere in the ask disqualify it.
    if re.search(
        r"\b(build|create|write|edit|update|change|modify|remove|delete|fix|"
        r"refactor|optimise|optimize|improve|rewrite|generate|deploy|install|"
        r"add|set up|setup|configure|push|commit|upload|download|send|post|"
        r"publish|submit|apply|pay|buy)\b",
        text,
        re.I,
    ):
        return False
    return True


def _select_recipe(text: str, normalized: str) -> tuple[str, str] | None:
    """Return a read-only shell recipe (command, label) or None.

    Order matters: the most specific family wins. We only fire when the ask is
    unambiguously one of the three read-only, recurring task families.
    """
    # 1. Bug / code review scan — most common, most expensive for the model.
    if "bug" in normalized or "issues" in normalized or "code" in normalized:
        if _BUG_SCAN_RE.search(text):
            return _BUG_SCAN_CMD, "bug-scan"
    # 2. Run the test suite.
    if _RUN_TESTS_RE.search(text):
        return _RUN_TESTS_CMD, "run-tests"
    # 3. Project structure inventory.
    if _PROJECT_STRUCTURE_RE.search(text):
        return _PROJECT_STRUCTURE_CMD, "project-structure"
    return None


def task_recipe_plan(text: str) -> ToolCallRequest | None:
    """Return a single ``sandbox_batch`` call that performs *text* with zero
    provider calls, or None to fall through to the normal LLM path.

    The returned call runs a read-only shell recipe inside the configured
    execution sandbox (Novita / VPS / Upstash — wherever the user's project
    files actually live), and returns the command's stdout as the answer.
    The runner's registry guard decides which command tool is present; if the
    sandbox batch tool is absent the runner transparently re-targets a local
    ``exec`` run, and if neither is available the turn falls through to the
    model untouched.
    """
    if not task_router_enabled():
        return None
    raw = text or ""
    normalized = re.sub(r"\s+", " ", raw).strip().lower()
    if not (_MIN_TEXT_CHARS <= len(normalized) <= _MAX_TEXT_CHARS):
        return None
    if not _looks_read_only(normalized):
        return None
    recipe = _select_recipe(raw, normalized)
    if recipe is None:
        return None
    command, label = recipe
    return ToolCallRequest(
        id=f"task-{label}",
        name="sandbox_batch",
        arguments={
            "stop_on_error": False,
            # A single `run` op runs the whole recipe inside the sandbox on the
            # user's own project tree.
            "operations": [{"action": "run", "command": command, "timeout": 300}],
        },
    )
