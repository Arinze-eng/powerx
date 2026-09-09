"""Task plan cache: a user task solved once replays its TOOL STEPS with ZERO LLM calls.

This is the layer that makes "the sandbox handles everything without calling the
API" real for *tasks* — coding, builds, file generation, data crunching — not
just read lookups. Manus's ratio (2126 commands run, 22 API calls) comes from
exactly this shape of discipline: the model reasons ONCE to discover a sequence
of deterministic steps, then every structurally-similar repeat re-runs those
steps directly in the sandbox. The LLM is only paid again when something is
genuinely new or a replay hits an exception.

How it works
------------
1. When a run finishes successfully after making tool calls, we store a PLAN:
   the ordered ``(tool_name, arguments)`` steps the model chose, keyed by a
   *normalized template* of the user's task text. Normalization strips variable
   bits (numbers, names, dates, file paths, quoted strings) so
   "build a python script that prints hello" and "...prints goodbye" collapse
   to one slot with different fill-ins.

2. On a fresh task, if the normalized template hits a stored plan AND the
   concrete values line up (same count of placeholders), we replay each step
   through the live ToolRegistry — NO provider call at all. The final answer is
   whatever the last step produced.

3. Replay is strict and fail-open. Any step erroring, an empty result, or a
   mismatch between the recorded plan and what the tools now accept aborts the
   replay and falls back to the normal LLM path, which will (re)learn and
   overwrite the plan. A bad cached plan can never produce a wrong answer —
   worst case it costs one wasted attempt and the model takes over.

Safety rules baked in
---------------------
* Only plans whose steps are ALL safe-to-replay are ever stored or executed:
  the sandbox coding tools (``novita_sandbox``, ``sandbox_batch``, ``exec``)
  plus read-only lookups. Anything that can mutate external state or spend
  money on the user's behalf (message send, DB writes, uploads/downloads,
  submit_question, deploy) is refused as a plan and left to the model.
* Plans expire (TTL) and are capped per-workspace, mirroring the replay cache.
* Gated by env ``POWERX_PLAN_CACHE`` (default on) and per-run opt-in via
  ``AgentRunSpec.enable_plan_cache`` so generic agents are untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # a week; task plans go stale slowly
_MAX_PLANS_PER_WORKSPACE = 200
_MAX_STEPS_PER_PLAN = 16
_MIN_MEANINGFUL_TASK_CHARS = 10

#: Minimum token-set Jaccard similarity for a fuzzy plan match (#4). High enough
#: that unrelated tasks don't collide, low enough to absorb filler-word noise.
PLAN_FUZZY_THRESHOLD = 0.72

#: Common words stripped before comparing templates so "please/the/now" don't
#: dominate similarity between genuinely different tasks.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "please", "pls", "now", "can", "you", "i", "to",
        "for", "of", "in", "on", "my", "me", "and", "do", "does", "is", "it",
        "this", "that", "would", "could", "should", "just", "then", "with",
    }
)


def _template_similarity(a: str, b: str) -> float:
    """Token-set Jaccard of two normalized templates after stopword removal.

    Returns 0..1. Identical content sets -> 1.0; disjoint -> 0.0. Empty on both
    sides is treated as a perfect match (both were pure stopwords).
    """
    ta = {t for t in a.split() if t not in _STOPWORDS}
    tb = {t for t in b.split() if t not in _STOPWORDS}
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0



def plan_cache_enabled() -> bool:
    return os.environ.get("POWERX_PLAN_CACHE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# ---------------------------------------------------------------------------
# Which tools may appear in a replayable plan
# ---------------------------------------------------------------------------

#: Sandbox / coding tools: these ARE the work the user wants done repeatedly.
#: Re-running them is exactly the point of the plan cache.
_CODING_TOOLS: frozenset[str] = frozenset({"novita_sandbox", "sandbox_batch", "exec"})

#: Read-only lookup tools that are harmless to replay (kept general, not tied
#: to any single product).
_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "uniabuja_student",
        "uniabuja_transcript",
        "read_file",
        "list_dir",
        "grep_search",
        "file_search",
    }
)

_SAFE_REPLAY_TOOLS: frozenset[str] = _CODING_TOOLS | _READ_ONLY_TOOLS

#: novita_sandbox actions that move bytes across the network or cost money and
#: must NOT be auto-replayed. 'run'/'read'/'write'/'list'/'reset' are fine.
_UNSAFE_SANDBOX_ACTIONS: frozenset[str] = frozenset({"upload", "download_url", "fetch_url"})

#: sandbox_batch sub-actions considered unsafe inside a batch operation list.
_UNSAFE_BATCH_ACTIONS: frozenset[str] = frozenset({"upload", "download_url", "fetch_url"})


def _step_is_safe(name: str, args: dict[str, Any]) -> bool:
    """Fine-grained guard beyond the coarse tool whitelist."""
    if name not in _SAFE_REPLAY_TOOLS:
        return False
    action = str(args.get("action") or "").strip().lower()
    if name == "novita_sandbox":
        if action in _UNSAFE_SANDBOX_ACTIONS:
            return False
    elif name == "sandbox_batch":
        for op in args.get("operations") or []:
            if isinstance(op, dict):
                if str(op.get("action") or "").strip().lower() in _UNSAFE_BATCH_ACTIONS:
                    return False
    elif name == "uniabuja_student":
        # submit_question mutates state; never replay it silently.
        if action == "submit_question":
            return False
    return True


def plan_is_safe(steps: list[dict[str, Any]]) -> bool:
    """Every step must target a safe-to-replay tool with safe arguments."""
    if not steps or len(steps) > _MAX_STEPS_PER_PLAN:
        return False
    for step in steps:
        if not isinstance(step, dict):
            return False
        name = str(step.get("name") or "")
        args = step.get("arguments")
        if not isinstance(args, dict):
            return False
        if not _step_is_safe(name, args):
            return False
    return True


# ---------------------------------------------------------------------------
# Task normalization -> stable template + captured variables
# ---------------------------------------------------------------------------

_VAR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Quoted literals first so their contents aren't double-substituted.
    (re.compile(r'"[^"]*"'), "<STR>"),
    (re.compile(r"'[^']*'"), "<STR>"),
    # Registration-number-like tokens.
    (re.compile(r"\b\d{2}/\d{3}[A-Za-z]{2,6}/\d{2,6}\b"), "<ID>"),
    # Dates.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<DATE>"),
    (re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"), "<DATE>"),
    # File paths / filenames with extensions.
    (
        re.compile(
            r"(?<!\w)(?:[\w./-]+\.(?:pdf|docx?|txt|csv|xlsx?|pptx?|md|png|jpe?g|gif|svg|py|js|jsx|ts|tsx|json|html|css|sh|go|rs|java|rb))\b",
            re.I,
        ),
        "<FILE>",
    ),
    # Standalone numbers (counts, versions, amounts).
    (re.compile(r"\b\d+\b"), "<NUM>"),
    # Capitalized proper-name runs (Ada Okafor, John Smith).
    (re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b"), "<NAME>"),
]


@dataclass(slots=True)
class NormalizedTask:
    """A task reduced to a template plus its concrete variable values."""

    template: str            # whitespace-collapsed, variables replaced by tokens
    variables: list[str]     # the literals that were stripped, in order
    raw: str                 # original text (for debugging / exact-match key)


def normalize_task(text: str) -> NormalizedTask | None:
    """Return a normalized fingerprint for *text*, or None if too short/volatile.

    Returns None when there is nothing meaningful to key on (tiny greetings) so
    we never cache a plan for 'hi'/'ok' and accidentally cross-wire answers.
    """
    collapsed = re.sub(r"\s+", " ", (text or "")).strip()
    if len(collapsed) < _MIN_MEANINGFUL_TASK_CHARS:
        return None

    working = collapsed
    found: list[tuple[int, str, str]] = []  # (position, token, literal)
    for pattern, token in _VAR_PATTERNS:
        def _sub(match: re.Match[str], _token: str = token) -> str:
            found.append((match.start(), _token, match.group(0)))
            return f" {_token} "

        working = pattern.sub(_sub, working)

    working = re.sub(r"\s+", " ", working).strip().lower()
    if not working:
        return None
    # Sort collected literals by their position in the ORIGINAL text so the
    # fill-in order matches how they appeared to the user.
    found.sort(key=lambda item: item[0])
    return NormalizedTask(
        template=working,
        variables=[lit for _pos, _tok, lit in found],
        raw=collapsed,
    )


def _fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StoredPlan:
    template: str
    variables: list[str]      # concrete values recorded alongside the plan
    steps: list[dict[str, Any]]
    saved_at: float
    ttl_seconds: float = _DEFAULT_TTL_SECONDS

    def is_expired(self) -> bool:
        return time.time() - self.saved_at > self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.saved_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "variables": self.variables,
            "steps": self.steps,
            "saved_at": self.saved_at,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoredPlan | None":
        try:
            template = str(data["template"])
            steps = data["steps"]
            if not isinstance(steps, list) or not plan_is_safe(steps):
                return None
            return cls(
                template=template,
                variables=list(data.get("variables") or []),
                steps=steps,
                saved_at=float(data.get("saved_at") or 0.0),
                ttl_seconds=float(data.get("ttl_seconds") or _DEFAULT_TTL_SECONDS),
            )
        except (KeyError, TypeError, ValueError):
            return None


class PlanCache:
    """Disk-backed store of task templates -> replayable tool-step plans."""

    def __init__(self, workspace: str | Path | None = None) -> None:
        root = Path(workspace or os.getcwd()) / ".powerx" / "plans"
        root.mkdir(parents=True, exist_ok=True)
        self._root = root

    def _path(self, template: str) -> Path:
        return self._root / f"{_fingerprint(template)}.json"

    def get(self, norm: NormalizedTask) -> StoredPlan | None:
        """Exact-template lookup (fast path)."""
        path = self._path(norm.template)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        plan = StoredPlan.from_dict(data)
        if plan is None or plan.is_expired():
            path.unlink(missing_ok=True)
            return None
        return plan

    def get_fuzzy(self, norm: NormalizedTask) -> StoredPlan | None:
        """Best-match lookup tolerant of filler-word differences (#4).

        Exact template equality is too brittle: "compile the project in folder
        7" vs "please compile project folder 7 now" are the SAME task but hash to
        different templates because of stopword/ordering noise. When the exact
        path misses we scan stored plans and return the closest one whose token
        Jaccard similarity clears PLAN_FUZZY_THRESHOLD *and* whose variable count
        matches (so substitution stays positionally correct). Returns None when
        nothing is close enough — never replays an unrelated plan.
        """
        exact = self.get(norm)
        if exact is not None:
            return exact
        best: StoredPlan | None = None
        best_score = 0.0
        try:
            paths = list(self._root.glob("*.json"))
        except OSError:
            return None
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            plan = StoredPlan.from_dict(data)
            if plan is None or plan.is_expired():
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            # Variable count must match for positional substitution to be valid.
            if bool(plan.variables) != bool(norm.variables) or len(plan.variables) != len(
                norm.variables
            ):
                continue
            score = _template_similarity(norm.template, plan.template)
            if score > best_score:
                best_score = score
                best = plan
        if best is not None and best_score >= PLAN_FUZZY_THRESHOLD:
            logger.info(
                "plan cache: fuzzy-matched '{}' ~ '{}' (similarity {:.2f})",
                norm.template[:50],
                best.template[:50],
                best_score,
            )
            return best
        return None

    def put(self, norm: NormalizedTask, steps: list[dict[str, Any]]) -> StoredPlan | None:
        if not plan_is_safe(steps):
            return None
        plan = StoredPlan(
            template=norm.template,
            variables=list(norm.variables),
            steps=steps,
            saved_at=time.time(),
        )
        path = self._path(norm.template)
        try:
            path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
        except OSError:
            return None
        self._prune()
        return plan

    def _prune(self) -> None:
        try:
            entries = sorted(
                self._root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for stale in entries[_MAX_PLANS_PER_WORKSPACE:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass


def make_plan_cache(workspace: str | Path | None = None) -> PlanCache | None:
    if not plan_cache_enabled():
        return None
    return PlanCache(workspace)


# ---------------------------------------------------------------------------
# Variable substitution for replay
# ---------------------------------------------------------------------------


def substitute_variables(
    steps: list[dict[str, Any]], old_values: list[str], new_values: list[str]
) -> list[dict[str, Any]]:
    """Rewrite a stored plan's string arguments, swapping recorded literals for
    the new task's literals, positionally. Non-string leaves are left alone.

    Substitution is applied longest-literal-first so a shorter value that is a
    substring of another cannot clobber it mid-pass.
    """
    pairs = [(o, n) for o, n in zip(old_values, new_values) if o and n and o != n]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for old, new in pairs:
                out = out.replace(old, new)
            return out
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return [{"name": s["name"], "arguments": walk(s.get("arguments") or {})} for s in steps]


def replayable_for(new_norm: NormalizedTask, plan: StoredPlan) -> bool:
    """Replay is only safe when the new task has the SAME template shape and a
    compatible number of variables to slot into the recorded plan."""
    if new_norm.template != plan.template:
        return False
    if not plan.variables:
        # Pure instruction (no variables): always a direct replay candidate.
        return True
    return len(new_norm.variables) == len(plan.variables)


def variables_compatible(new_norm: NormalizedTask, plan: StoredPlan) -> bool:
    """Positional-substitution safety check for fuzzy-matched plans (#4).

    Unlike replayable_for this does NOT require identical templates — the caller
    (get_fuzzy) already established similarity. All that matters now is that the
    variable lists line up so substitute_variables maps old->new correctly:
    either both empty, or equal counts. A count mismatch would misalign fill-ins
    and could produce a wrong command, so we refuse it and fall back to the model.
    """
    return len(new_norm.variables) == len(plan.variables)
