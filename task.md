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
