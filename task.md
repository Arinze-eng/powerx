# Task — admin feature gates + separate broker method

Decisions (2026-10-15, from the user's answers):
1. General-task switch: **global kill switch + per-user override** (tri-state override:
   inherit / force-allow / force-deny).
2. MT5 (Wine) switch: **off for everyone, admins included** — pure global kill switch.
3. Broker method: **direct MT5 server login with the user's own login/password/server**,
   broker build auto-installed in the sandbox, as a NEW `broker_trade` flow that leaves
   `mt5_sandbox` untouched.
4. Brokers section: **per-user credentials in Settings**, plus a global admin default/override
   on the admin page.
5. Finish: commit + push to main, then deploy to Northflank.

## Files

| File | Change |
|---|---|
| `nanobot/config/schema.py` | `AccessConfig` (general_task_enabled, mt5_trading_enabled) on root `Config` |
| `nanobot/access_policy.py` | NEW — resolve global + per-user overrides, JSON-backed store |
| `nanobot/agent/hooks/access_gate.py` | NEW — `AccessGateHook` refuses a general task at turn start |
| `nanobot/agent/hooks/__init__.py` | export the new hook factory |
| `nanobot/cli/gateway_runtime.py` | register the access-gate hook |
| `nanobot/agent/tools/mt5_sandbox.py` | honour the MT5 kill switch |
| `nanobot/admin_registry.py` | "Access control" section + routes + save |
| `nanobot/webui/ws_http.py` | action map for the new admin routes |
| `supabase/broker_credentials.sql` | NEW table (mirrors alpaca_credentials.sql) |
| `nanobot/trading/broker_credentials.py` | NEW — encrypted per-user broker credential store |
| `nanobot/agent/tools/broker_trade.py` | NEW — the separate broker flow |
| `nanobot/webui/brokers_api.py` | NEW — Settings → Brokers REST/WS endpoints |
| `webui/src/...` | Settings → Brokers section |
| `tests/...` | unit tests for the policy, the gate, the store |

## Status
- [x] config + policy + hook
- [x] admin UI + routes
- [x] mt5 gate
- [x] broker store + tool + settings API
- [x] frontend Brokers section
- [x] tests
- [x] push + deploy
- [x] run `supabase/broker_credentials.sql` (see "Database" below)

## Notes
- The general-task switch exempts a turn that either declares itself trading
  (`turn_kind="trading"`) or whose opening message reads as a trade
  (`message_is_trading`). Without the second signal a trading user would be
  refused before the model could ever reach `broker_trade`.
- `broker_trade` reuses `mt5_sandbox`'s CLI plumbing (command build, bootstrap,
  JSON extraction, sandbox resolution) by import; the sandbox tool itself is
  unchanged.

## Database
The table has to exist in the Supabase project the *deployed* service points at.
`SUPABASE_URL` on Northflank (project `cdnai`, service `powerx`) is
`https://mitrbmjxriqvfacaefvg.supabase.co` — that is the production project, and
it is the one that matters. Its pooler region is `aws-1-eu-west-1`.

`supabase/broker_credentials.sql` was executed and verified against
`fmsytqzjixcumjnzishm.supabase.co` (the credentials pasted in chat), which is a
different project holding another app's schema. Verified there: 10 columns,
`UNIQUE(user_key, login, server)`, `updated_at` trigger, RLS enabled,
`service_role` grants, and a REST insert/select/delete round-trip
(201/200/204) leaving the table empty. The file is now re-runnable
(`DROP POLICY IF EXISTS` + `DROP TRIGGER IF EXISTS`) and carries the
`service_role` grants it needs — without them PostgREST answers
`42501 permission denied for table broker_credentials`.

### Production run — done
`broker_credentials` now exists in the project the deployed service actually uses,
`mitrbmjxriqvfacaefvg` (`aws-1-eu-west-1` pooler). Created with the file as
committed; verified in the database (10 columns, `UNIQUE(user_key, login, server)`,
`updated_at` trigger, RLS on, service_role grants, `idx_broker_credentials_user_key`)
and then over the live REST API, and finally by running the shipped
`BrokerCredentialStore` itself against it: store / list / decrypt / switch default /
re-save-corrects-password / partial delete / full delete, 16 checks, all pass, table
left empty. `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and
`SUPABASE_TOKEN_ENCRYPTION_KEY` on the Northflank service already point at that
project, so no env change was needed.

## Fix round 2 — the "unknown action" error and the admin switches

### The bug: Settings -> Brokers never reached its own handler
`settings_routes._route()` mapped the four broker paths to the `brokers` domain
(`/api/settings/brokers` -> `list`, `.../save` -> `save`, `.../delete` -> `delete`,
`.../test` -> `test`), but the dispatch chain in `WebUISettingsRouter.dispatch`
had no `elif domain == "brokers"` branch. Every broker request therefore fell
through to the chain's `else`, which hands the action to the **system** domain.
`settings_system.handle` knows no `list`/`save`/`delete`/`test` action and answers
`404 {"error": "unknown settings action"}` — the error the pane showed when a user
pressed *Save account*. `_handle_brokers` (settings_routes.py:904) was dead code:
nothing called it, so the whole Brokers feature was unreachable over the mutation
channel and over the plain read path alike.

Reproduced at the router level before the fix (all four paths, 404 + that exact
message), and the same code is what `0b609e1` deploys.

Fixed by adding the missing branch. After it, all four routes answer 200 and a
saved credential round-trips: `save` returns `last_action: "saved"` with the row
(`login`, `server`, `has_password: true`, no password), `list` returns it, `test`
resolves the server profile, `delete` returns an empty list.

### The admin switches — verified live, not changed
Driven over the real production admin socket (Basic-auth `/api/admin/ws-bootstrap`
-> `wss://...?token=...&client_id=admin` -> `{type:"webui_request", action:
"admin.access.save"}`): all three switches flipped off, read back off over
`GET /api/admin/access-settings`, restored, read back on; a per-user override set
and cleared. Production is left exactly as it was (all three switches on, no
overrides). So the switches themselves work; what looked broken was the broker
flow they gate, which was unreachable for the reason above.

### Seeing the credential truly
`BrokersSettings` now re-reads the account list from the server after a save
instead of trusting the mutation's own echo, and says so plainly ("Saved and
stored: <login> on <server>") — or shows an error if the row did not come back.
Each account row also states whether a password is stored ("password stored
(encrypted)" / "no password stored"), which is what the user asked to be able to
see.

### Tests
- `tests/webui/test_settings_routes.py`: every broker route reaches the broker
  handler (fails on `0b609e1`), an unauthenticated save is refused and writes
  nothing, and a structural guard that every domain `_route` can return has a
  dispatch branch — the class of bug this was.
- `tests/test_access_policy.py`: the admin access routes through `admin_route`
  itself — auth required, all three switches read/write/read-back, a partial write
  leaves unrendered switches alone, overrides set and clear, a keyless override is
  a 400.
- `tests/test_access_policy.py` + `tests/test_broker_method.py` +
  `tests/webui/test_settings_routes.py`: 62 passed. Full `tests/webui`: 503 passed,
  13 failed — the same 13 that fail on a stashed tree (attachment ingress, codex
  OAuth, build, version shape), so pre-existing and untouched by this change.
- `npx tsc -p tsconfig.build.json --noEmit` clean; `npm run build` succeeds.

## Fix round 3 — the save that times out after it has already been written

The user reported the save still failing *after* `a192b2d` deployed: "when user
enter credentials and want to save it not working and timing out". Different
failure, same pane.

### What the evidence says
- The write lands. Read straight off production PostgREST: one row for the
  signed-in user (`user_key c9c56243-…`, `login 41261482`, `server Deriv-Demo`,
  `id 6`, `updated_at 05:28:35Z`). So the server side of the save works; the
  client gives up before the reply arrives.
- No broker error is in the production logs at all in the 90 minutes spanning
  that save, and `_execute_webui_request` answers `500` (never a timeout) if the
  handler throws, so the failure is not a server error.
- A save was **four** sequential statements (POST, PATCH, PATCH, GET list), each
  on a **fresh `httpx.AsyncClient`** — four DNS lookups and four TLS handshakes.
  From the sandbox that was 1.9s cold / 1.2s warm; the container is 0.2 vCPU and
  its egress is the part that does not transfer.
- Mutations on one socket are serialized by a per-connection `asyncio.Lock`, and
  a reconnect mid-save replays the frame while the client's 20s timer keeps
  running.

### What changed
- `nanobot/trading/broker_credentials.py`: one pooled `httpx.AsyncClient` per
  store (per event loop, so a test's second `asyncio.run` does not inherit a
  closed loop), and a save with "make default" now clears the flag **before** the
  write instead of patching twice after it — three statements, not four, and no
  window in which two accounts are both the default. Cold 1.9s -> 1.3s, warm
  1.2s -> 0.5s against production Supabase.
- `nanobot/webui/brokers_api.py`: `_store()` keeps one store per process so the
  TLS connection survives between actions.
- `nanobot/channels/websocket/runtime.py`: a mutation over
  `_SLOW_WEBUI_MUTATION_LOG_MS` logs `waited_ms` (the lock) separately from
  `duration_ms` (the handler) — the line that says whether the user waited on the
  store, on the queue, or on the loop. The store logs its own slow statements.
- `webui/src/lib/api.ts`: broker save/delete get `BROKER_MUTATION_TIMEOUT_MS`
  (45s). Giving up does not undo a write, so the budget is set to outlast the
  work rather than to bound it.
- `webui/src/components/settings/brokers/BrokersSettings.tsx`: the stored list is
  the only thing that decides success. A mutation that timed out is not reported
  as failed when the row is there — the pane says it saved and that it checked.
  Same for a delete whose row is gone. The read-back retries once.

### Tests
- `tests/test_broker_method.py`: a real store on a recording HTTP client — a
  default save is `["PATCH", "POST"]` with the clear first, a plain save is one
  statement, two saves on one loop build one client, a second loop rebuilds it,
  and `_store()` is a singleton. 20 passed.
- `nanobot/channels/websocket/tests/test_websocket_channel.py`: a slow mutation
  logs one line carrying the action, status, wait and duration. (That file has a
  pre-existing failure — a stale `assert_awaited_once_with` missing
  `supabase_user_id=''` — and a test after it that hangs; both reproduce on a
  clean `a192b2d` worktree.)
- `tests/webui tests/test_broker_method.py tests/test_access_policy.py`: 507
  passed, 14 failed — the same 13 as before plus
  `test_session_list_index.py::test_webui_session_scan_does_not_overlap_session_save`,
  which passes on its own in both trees (thread-overlap timing test, flaky under
  load).
- `npx tsc -p tsconfig.build.json --noEmit` clean; `npm run build` succeeds.

## Fix round 4 — the 401 the pane was really showing (found in a real browser)

Drove the actual pane in Chrome (a throwaway Supabase user, a seeded session, the
real site): Settings -> Brokers renders, the form fills, Save is clicked. The
save itself works end to end — the frame goes out over the authenticated socket
and the answer comes back `ok: true` with the row in **1.8s** — but the pane
answered **"Sign in to manage broker accounts"** for a signed-in user, and the
console showed the reads failing 401.

Cause: `fetchBrokerAccounts` / `testBrokerServer` sent only
`Authorization: Bearer <gateway token>`. The broker routes key every action off
the signed-in Supabase user, which the gateway reads from `X-Nanobot-Auth`
(`_request_user_id` -> `_supabase_user_id_for_request`), so the read arrived
anonymous and `_require_user` refused it — while the save, which travels over the
socket and carries the identity, landed fine. The row was there and the pane said
it was not signed in.

That also matters for round 3's verify-by-read: a read-back that 401s would report
a landed save as failed.

- `webui/src/lib/api.ts`: both broker reads take `authValue` and send
  `X-Nanobot-Auth`, the same way `listSessions` already does.
- `BrokersSettings.tsx`: passes `authValue`/`getAuthValue()` from `useClient`.
- `webui/src/tests/api.test.ts`: both reads carry the identity, and neither sends
  the header when there is no Supabase session.

Measured in the deployed container (throwaway user, real socket):
list cold 5.1s (first broker action in a cold process), save cold 0.90s, save
warm 0.43s, list warm 0.18s, delete 0.53s. Container egress to Supabase is not
slow, so the store was never what a 20s client timeout was waiting on.

## Fix round 5 — the credential the pane saved was unreachable by the tool that trades it

**Symptom.** "credential me I'll can't see it ... use that tool to trade real time ... exness and deriv test it trade".

**Root cause (found, and it is the whole of "use that tool to trade").** One
signed-in user reached the credential table under two spellings of one identity.
Settings -> Brokers keys off the Supabase user id the socket authenticated with
-- the bare `<uuid>` (verified in production: row id 6 is
`user_key = c9c56243-7cd3-4fa5-b91d-736ee4364b16`). `broker_trade` keys off
`nanobot.access_policy.turn_user_key`, which namespaces the same id as
`supabase:<uuid>`. Stored verbatim those are two different rows, so the tool
answered `broker_not_connected` for a user looking at the account on screen.

Fix: `broker_credentials.credential_user_key()` folds the `supabase:` prefix
away at the storage boundary, so both callers reach one row and neither has to
know the other's shape. A Telegram key (`telegram:<id>`) is untouched.

**Verified against production Supabase, before deploying:**
```
pane   list(bare)         -> [('41261482', 'Deriv-Demo', '')]
tool   get(supabase:uid)  -> login=41261482 server=Deriv-Demo password_len=14
record_connection         -> True [('41261482', 'connected', '2026-10-17T07:38:43+00:00')]
```

**"Know when it connected."** Whether an account is *connected* is a fact only
the terminal knows, and the terminal only runs inside the execution sandbox --
so the settings pane could never have answered it. `broker_trade` now writes the
outcome of every action that proves a login (`connect`/`account`/`positions`/
`order`/`close`) onto the row (`connection_status`/`connection_message`/
`connection_at`), and the pane reads it back as a status badge: green
"Connected <time>", red "Not connected -- last login failed: <reason>", grey
"Not connected yet -- connect from a chat". An account that is saved but never
used is never reported as connected. `install`/`status`/`doctor` are excluded on
purpose: a working install is not a login.

Migration applied to production and committed in `supabase/broker_credentials.sql`
(additive, `ADD COLUMN IF NOT EXISTS`); `list_credentials` falls back to the
pre-column select on a 42703 so a deployment that has not run it still lists.

Also: `broker_trade` now resolves the process-wide store
(`broker_credentials.shared_store()`), the same instance the pane uses, so a
trade inherits its pooled Supabase connection instead of paying a fresh DNS
lookup and TLS handshake per call.
