# Daytona Network Access

PowerX sandboxes run on [Daytona](https://daytona.io). Daytona enforces a per-sandbox
outbound firewall, and this document explains how PowerX configures it and how to
extend it.

## How the egress policy works

Daytona accepts exactly one of these per sandbox (they are mutually exclusive;
sending both returns a `400`):

| Setting | Meaning |
| --- | --- |
| `networkAllowList` | Comma-separated IPv4 CIDR ranges. `0.0.0.0/0` opens all IPs but still only unlocks Daytona's *essential services* (package registries such as PyPI/GitHub) - general domains remain blocked. |
| `domainAllowList` | Comma-separated ASCII domains with optional `*.` wildcard prefixes (e.g. `example.com,*.google.com`). **This is the lever that actually unlocks arbitrary domains.** A bare `*` is rejected by the API. |
| `networkBlockAll` | Blocks all outbound traffic. |

Verified against the live API: a sandbox created with `networkAllowList: 0.0.0.0/0`
can reach `pypi.org` and `github.com` but NOT `example.com` or `google.com`; a
sandbox created with an explicit `domainAllowList` reaches every listed domain.

## PowerX defaults (`nanobot/agent/tools/daytona_backend.py`)

1. **Explicit custom `domain_allow_list`** (anything other than `*`) is sent as
   `domainAllowList` verbatim.
2. **`domain_allow_list: "*"`** or a **custom `network_allow_list`** (non-default
   CIDR) is sent as `networkAllowList` (open CIDR).
3. **Nothing configured** sends a built-in `DEFAULT_DOMAIN_ALLOW_LIST` (99 domains —
   Daytona rejects any allow list with more than **100** domains with HTTP 400, so
   custom lists must stay under that cap) covering: PyPI/npm/Go/Crates/Ruby/Maven
   registries, runtimes/SDKs/CDNs (Node, Yarn, Bun, Google SDK hosting, jsDelivr,
   unpkg, esm.sh, Microsoft/LLVM/launchpad apt repos), GitHub/GitLab, all major AI
   provider APIs (OpenAI, Anthropic, Gemini, DeepSeek, Groq, Mistral, xAI, Together,
   Fireworks, Perplexity, Cohere, HuggingFace), search engines, Ubuntu/Debian mirrors,
   container registries, Telegram/Discord/Slack APIs, file-sharing hosts, and
   connectivity diagnostics endpoints.

Note: an allow list applies to **newly created** sandboxes only — delete an existing
sandbox (admin reset) so a task picks up the updated policy. Daytona organizations
on Tier 1/2 cannot override network restrictions at sandbox level; check the
dashboard tier if a needed domain stays blocked.

## Fetch tool allowlist

The sandbox `fetch_url` tool has its own host allowlist. Defaults include the file
hosts plus raw.githubusercontent.com, files.pythonhosted.org, registry.npmjs.org and
common mirrors. Override it with:

- Admin UI field `daytonaFetchAllowHosts` (config key `fetch_allow_hosts`), or
- Environment variable `NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS` (Supabase setting key
  `nanobot_NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS`).

Values are comma-separated domains with optional `*.` wildcards, or a single `*` to
allow every HTTPS host.

## Configuration surface

| Source | Key / env var |
| --- | --- |
| Supabase `system_settings` | `nanobot_NANOBOT_DAYTONA_API_KEY`, `nanobot_NANOBOT_DAYTONA_API_URL`, `nanobot_NANOBOT_DAYTONA_SNAPSHOT`, `nanobot_NANOBOT_DAYTONA_DOMAIN_ALLOW_LIST`, `nanobot_NANOBOT_DAYTONA_NETWORK_ALLOW_LIST`, `nanobot_NANOBOT_DAYTONA_FETCH_ALLOW_HOSTS`, `nanobot_NANOBOT_DAYTONA_TTL_MINUTES` |
| Northflank env | Same variable names without the `nanobot_` prefix (e.g. `NANOBOT_DAYTONA_DOMAIN_ALLOW_LIST`) |
| Config file | `execution.daytona.{domain_allow_list,network_allow_list,fetch_allow_hosts}` |

Note: allowlist changes only apply to **newly created** sandboxes. Delete an
existing sandbox (admin reset) to have the new policy take effect.

## Daytons tier note

Daytona's docs state Tier 1/2 organizations have network restrictions that cannot be
overridden at sandbox level; Tier 3/4 default to full internet. If a domain you need
stays blocked even after adding it to the allowlist, check the organization tier in
the Daytona dashboard.
