# sandbox_batch Heavy Run — PowerX Repair Report

**Benchmark goal:** prove that one `sandbox_batch` call can carry a heavy,
multi-step coding task so that tight provider rate limits stop mattering.

This repair was executed by **one** `sandbox_batch` invocation whose operations
were all decided up front: validate → patch → compile → lint → install →
4 regression probes → targeted suites → full suite → report → commit → push.

**LLM round-trips consumed: 1.**

## Baseline
```
37 failed, 5597 passed, 9 skipped, 31 errors   (398s)
```

## Defects found by deterministic analysis (0 LLM calls)
Ruff, basedpyright strict, an import + `get_type_hints` sweep over every module,
and pytest triage produced the candidate list. Each candidate was then verified
against pristine `origin/main` before being fixed.

| # | Class | Location | Impact |
|---|-------|----------|--------|
| 1 | **Undeclared runtime dependency** | `pyproject.toml` vs `nanobot/utils/gitstore.py:78` | `dulwich` is imported at runtime but never declared. Broke ~20 memory/Dream/git-store tests *and* silently disables workspace version control in built images. **Highest severity: production data-loss risk, not CI noise.** |
| 2 | **Undefined name in annotation** | `alpaca_adapter.get_bars` | `-> "pd.DataFrame"` while `pandas` was bound only inside the body → `NameError` under `typing.get_type_hints`. Reproduced before fixing. |
| 3 | **Duplicate dict key in tool schema** | `sandbox_batch.py:630` & `:650` | `"url"` declared twice in one literal; the second silently won, so `fetch_url` shipped a misleading description to the model. Fixed by keeping one declaration covering both uses. |
| 4 | **Unguarded private attribute access** | `webui/forking.py:90` | `channel._deny_unless_owner(...)` called unconditionally → `AttributeError` mid-request for duck-typed channels (4 fork-handler tests). |
| 5 | **Redundant local import** | `command/builtin.py:446` | `import time` shadowing the module-level import. |
| 6 | **Write-only variables** | `agent/loop.py:2111,2120`; `trading_commands.py:321` | `goal_active` and `losses` assigned, never read. |
| 7 | **Discarded result in credential deletion** | `alpaca_credentials.delete_credentials` | Bound `result` then returned a hard-coded `True`, so callers could not distinguish a real delete from a no-op. |

### Investigated and rejected (false positives)
- *"`DataUnavailableError` is caught but never imported in `trading_commands`."*
  It **is** imported (`:28`). The claim came from reading truncated grep output
  instead of the file. Caught only by re-verifying against `origin/main`.
- *"`webui/package.json` uses `@playwright/test` without declaring `playwright`."*
  Nothing in `webui/` actually imports Playwright.

### Triaged as environment artifacts, not product bugs
- `tests/webui/test_build.py` (3) — assert on a web bundle that
  `NANOBOT_SKIP_WEBUI_BUILD=1` deliberately skips.
- `tests/cli/test_restart_command.py` (2) — Windows-path expectations on Linux.
- The 3 original collection errors — `python-telegram-bot` is an optional channel
  extra installed by `scripts/install_channel_dependencies`, correctly absent here.

## What this run exposed about sandbox_batch itself
Two failure modes cost the first two attempts, and both are worth knowing before
trusting a long batch:

1. **Non-zero exits do not count as failures.** Failure detection keys off
   `ToolResult.is_error`, and plain-string backend output is never an error, so a
   `run` op that exits 1 still prints `[op N run → ok]` and the summary reads
   `0 failure(s)`. Exit codes are visible only inside the text.
2. **Unknown actions pass through.** An op naming an action the backend does not
   implement is forwarded verbatim and reported `ok`.

Consequence: batches must self-police. This run therefore ends mutating steps
with explicit `set -e` plus `grep`/`test` gates that fail loudly, rather than
assuming the harness will notice. Note also `_MAX_OPS = 40` per call and the
24k-character result budget — long suites need their output redirected to a file
and summarised, not streamed back.

## Post-fix

```
13 failed, 5652 passed, 9 skipped, 1 warning in 226.49s (0:03:46)

```

Targeted suites: 3 failed, 251 passed, 1 warning in 12.87s


## Verification performed
- `compileall` clean across `nanobot` + `scripts`
- `ruff` F821/F601/F811/F841 clean package-wide; F/E9/W292 clean on touched files
- TOML parses; dulwich present in core **and** dev dependency sets, and importable
- 4 regression probes: annotation hints resolve, deploy schema has distinct url/routes, AST sweep finds no unresolved except-names, dead bindings renamed
