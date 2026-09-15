# Daytona Network Access

PowerX sandboxes run on [Daytona](https://daytona.io). Daytona enforces outbound
network policy, and **which policy you are allowed to set depends on your
organization's billing tier.** Read the tier section first — on a restricted tier
no allow list can widen access, and that is not a PowerX bug.

Official reference: <https://www.daytona.io/docs/en/network-limits>
(saved copy: `docs/daytona-network-limits-official.md`)

## Tier determines what is possible

| Org tier | Sandbox-level network policy | Default egress |
| --- | --- | --- |
| **Tier 1 / 2** | **Rejected.** The API returns HTTP 400 for `domainAllowList`, `networkAllowList`, and `POST /sandbox/{id}/network-settings`. | Daytona *essential services* only. |
| **Tier 3 / 4** | Accepted on **newly created** sandboxes. | Full internet by default. |

Verified against the live API on a Tier 1/2 organization:

```
POST /sandbox {"domainAllowList": "example.com,..."}  -> 400
   "Network access is restricted and cannot be overridden at the sandbox
    level. Remove domainAllowList from the request."
POST /sandbox {"networkAllowList": "0.0.0.0/0"}       -> 201 (accepted, ineffective)
POST /sandbox/{id}/network-settings                    -> 400
Measured egress from inside the sandbox:
   pypi.org 200 · github.com 200 · registry.npmjs.org 200
   api.telegram.org 302 · api.anthropic.com 404
   example.com 000 (blocked) · google.com 000 (blocked)
```

So on Tier 1/2, package installs, `git`, and model APIs work, while fetching an
arbitrary website from **inside** the sandbox does not.

### How PowerX handles a restricted tier

`DaytonaExecutionBackend` sends the allow list first. If Daytona answers with the
tier rejection, PowerX:

1. remembers that for the life of the process, so later sandboxes do not each pay
   a guaranteed-failed create;
2. recreates the sandbox **without** any sandbox-level network key, so the user
   still gets a working essential-services sandbox instead of a hard failure;
3. logs a warning naming the dropped keys.

`networkAllowList` and `domainAllowList` are **mutually exclusive** — PowerX never
sends both (doing so is a `400`).

### Widening egress on a restricted tier

Two supported options:

* **Upgrade the organization tier** in the Daytona dashboard (full internet on
  Tier 3/4, plus working allow lists).
* **Route through your own proxy.** `outboundProxyUrl` IS accepted on Tier 1/2 and
  is the only lever there that reaches arbitrary hosts:

  ```
  NANOBOT_DAYTONA_OUTBOUND_PROXY_URL=http://user:pass@your-vps:3128
  ```

  The proxy is operator-run; you decide which hosts it forwards, so it becomes the
  real enforcement point.

## Entry limits

| Key | Max entries | Format |
| --- | --- | --- |
| `domainAllowList` | 100 | domains, optional `*.` wildcard prefix |
| `networkAllowList` | **10** | IPv4 CIDR blocks, `/prefix` required on every entry |

PowerX rejects oversized/malformed lists locally with a clear message instead of
letting Daytona return an opaque 400. Custom domain lists over 100 entries are
truncated with a warning.

## PowerX defaults (`nanobot/agent/tools/daytona_backend.py`)

1. **`domain_allow_list: "*"`** → sent as `networkAllowList: "0.0.0.0/0"`
   (Daytona's expression of open egress; mutually exclusive with a domain list).
2. **Explicit custom `domain_allow_list`** → sent verbatim as `domainAllowList`.
3. **Nothing configured** → the curated `DEFAULT_DOMAIN_ALLOW_LIST`, covering
   PyPI/npm/Go/Crates/Ruby/Maven registries, runtime and distro mirrors,
   GitHub/GitLab, major AI provider APIs, search, container registries,
   Telegram/Discord/Slack, and file-sharing hosts.

Remember that on Tier 3/4 setting an allow list is **restrictive**: it replaces the
default policy rather than adding to it, so include every host you need.

## Allow lists apply to new sandboxes only

Daytona applies network settings at creation. After changing a list, reset the
session's sandbox (admin reset) so the next task creates a sandbox that carries the
new policy.

## Fetch tool allowlist

The sandbox `fetch_url` tool enforces its **own** host allowlist, independent of
the Daytona firewall. Defaults cover the file hosts plus raw.githubusercontent.com,
files.pythonhosted.org, registry.npmjs.org and common mirrors. Override with:

* Admin UI field `daytonaFetchAllowHosts` (config key `fetch_allow_hosts`), or
* `NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS` (Supabase key
  `nanobot_NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS`).

Values are comma-separated domains with optional `*.` wildcards, or `*` for any
HTTPS host. Note this gate cannot rescue a request that Daytona's firewall blocks
below it.

## Configuration surface

| Source | Key / env var |
| --- | --- |
| Supabase `system_settings` | `nanobot_NANOBOT_DAYTONA_API_KEY`, `nanobot_NANOBOT_DAYTONA_API_URL`, `nanobot_NANOBOT_DAYTONA_SNAPSHOT`, `nanobot_NANOBOT_DAYTONA_DOMAIN_ALLOW_LIST`, `nanobot_NANOBOT_DAYTONA_NETWORK_ALLOW_LIST`, `nanobot_NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS`, `nanobot_NANOBOT_DAYTONA_TTL_MINUTES`, `nanobot_NANOBOT_DAYTONA_OUTBOUND_PROXY_URL` |
| Northflank env | Same names without the `nanobot_` prefix |
| Config file | `execution.daytona.{domain_allow_list,network_allow_list,fetch_allow_hosts,outbound_proxy_url}` |

The credential overlay (`nanobot/execution_env.py`) only fills values the config
file leaves empty, so an administrator's saved setting is never overwritten by a
durable deployment environment variable.

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `HTTP 400 ... cannot be overridden at the sandbox level` | Tier 1/2 org | Expected; PowerX drops the policy and continues. Upgrade tier or set `outbound_proxy_url`. |
| Domain blocked though it is in the allow list | Tier 1/2, or sandbox created before the change | Check tier; reset the sandbox. |
| `networkAllowList` rejected | >10 entries, or missing `/prefix` | Use ≤10 CIDRs, each with a prefix. |
| Both list keys sent | Mutually exclusive | Set one. |
| TLS reset reaching an OTLP/telemetry host | Custom list omitted `.daytona.io` | Add `.daytona.io`; the daemon's exporter needs it. |
| `Selected execution backend (x) is not ready` | Backend label and credentials disagree | Re-save the backend in the admin panel; the selection is now durable. |
