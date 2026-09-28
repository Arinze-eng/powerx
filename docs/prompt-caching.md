# Prompt caching on OpenAI-compatible endpoints

Every tool call in a task re-sends the whole conversation, so the prompt total
for a turn is the *sum* of each iteration's prompt. A 12-call turn measured in
production on 2026-09-27 cost **652,199 prompt tokens** with the completion flat
at ~90 tokens per call — the conversation was being re-sent in full, twelve
times. That is the shape of the bill whether or not anything is cached, so the
only way to know what you actually paid for is to record how much of each
prompt the gateway served from its cache.

## Three mechanisms, not one

| mode | what it does | when it is safe |
| --- | --- | --- |
| `auto` (default) | Nothing is added to the request. The gateway hashes the stable leading bytes (system message, tool schema, history) and bills a repeat against its stored copy. | Always. A hit is visible only in `usage`. |
| `markers` | Adds Anthropic-style `cache_control: {"type": "ephemeral"}` breakpoints to the system message, the second-to-last message and the tail of the tool list. | Only on an endpoint verified to accept them. |
| `off` | Sends nothing cache-related, including no markers for Claude. | When you want the request byte-identical to an uncached one. |

In every mode except `off` the request also carries `prompt_cache_key`, which is
not a cache format at all — see [the routing key](#the-routing-key-which-node-holds-the-prefix).

All three were probed live before this was written.

**Automatic prefix caching works and is reported.** Against `kymaapi.com`
(`qwen3.7-flash`), the same ~9k-token prefix sent twice returned
`prompt_tokens: 9045, prompt_tokens_details.cached_tokens: 8960` — a **99% hit**
— and the reported cost fell from `0.0003733` to `0.0000830`, a **78% cut**. No
request change was needed.

**Markers can silently destroy context.** Against the endpoint in use
(`gemini-proxy.codebanana.app`, `gemini-3.1-flash-lite`), a marked system message
came back **HTTP 200** while the model stopped receiving it at all:
`prompt_tokens` collapsed from `4234` to `14` and the model could no longer
recite a fact that existed only in the system prompt. Nothing raised, nothing
logged a warning. A *rejected* request is at least loud; a *swallowed* one is
not, which is why markers are opt-in per provider and verified with a recital
before anyone relies on them.

## The pool was throwing the cache away

Neither mechanism survives a pool that rotates **per request**. A cache lives on
one upstream account and is keyed on the exact leading bytes of the request, and
the admin pool holds up to 40 lanes across different base URLs and keys — so
turn 1 on lane A, turn 2 on lane B, turn 3 on lane C meant every lane was handed
a prefix it had never seen. Lane B cannot answer with lane A's cached copy, so
the 99% hit above is unreachable unless the same conversation keeps reaching the
same lane.

`PoolProvider` is therefore **sticky per conversation**. A conversation keeps the
lane it started on; a lane failure moves it, and it moves to the lane that
actually answered, so the cache follows the conversation rather than the pool.
A brand-new conversation still takes the next lane in turn, so load still
spreads — the rotation moved from per-request to per-conversation. Failover is
untouched: the pin only decides who is tried *first*, lanes parked by a recent
failure still go to the back, and when every lane is parked the full order is
returned anyway.

The pin is process-wide and keyed on the lane set, not held on the provider
instance, because the provider snapshot is rebuilt whenever the configured lanes
change and a pin that died with the instance would throw the cache away at that
exact moment. A pin whose lane has left the pool is dropped rather than honoured.

`PROVIDER_POOL_STICKY=0` restores the old per-request rotation, for anyone who
would rather have the spread than the cache. With no conversation bound — a cron
job, a bare script — rotation is what you get either way.

## The routing key: which node holds the prefix

Prefix caching is per machine. A gateway that runs several replicas behind one
URL matches a prefix only on the replica that already served it, so a
conversation that lands on a different node each turn has the same problem as a
pool that rotates each turn — every node sees a cold prefix, and a gateway that
reports no cache field gives you no way to tell.

OpenAI solves this with `prompt_cache_key`: the client names the conversation,
the gateway keeps it on the node that holds its prefix. nanobot now sends it on
every request except in `off`, derived from the same session key the agent loop
binds for the turn, hashed to 32 hex characters (`conversation_cache_key`). The
same session therefore produces the same key for the life of the conversation —
turn 2 and the retry of turn 2 both land where turn 1 did — while an unbound
caller (a cron job, a bare script) sends nothing, which is correct because it
has no conversation to keep together.

This is the only lever that exists against a gateway that reports nothing. What
is measured and what is not, on the endpoint currently configured
(`gemini-proxy.codebanana.app`, `gemini-3.1-flash-lite`):

* **Measured:** the field is *accepted*, on the real request body built by the
  provider's own `_build_kwargs` — HTTP 200, stable across two turns of one
  conversation, absent under `off`, absent with no bound conversation, 32 chars,
  and the model still recites a fact that exists only in the system prompt.
* **Not measured:** a hit rate. The endpoint's usage block is
  `{"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}` and
  nothing else — it reports no `cached_tokens` at all, so a hit on it cannot be
  observed from outside. Unknown fields are accepted there too (an invented one
  also returned 200), so the field is not being rejected; whether the proxy
  routes on it is **unproven on this endpoint**, and this page will not claim a
  saving it has not seen. On an endpoint that does report cache usage — the Kyma
  measurement above, or **Test caching** on any gateway that returns
  `cached_tokens` — the hit rate is visible and is the number to trust.

Sending it costs one bounded string on the wire, so it is on by default: a
gateway that ignores the field is unaffected. If a gateway *refuses* it, the
refusal is recognised and the call is retried once without any cache field at
all, the same path that makes `markers` survivable.

## The system prompt was in front of the conversation

A cache matches the longest common *leading* prefix, and the system message sits
in front of every message of the conversation. Two bands in that message describe
recency **state** rather than instruction — the durable artifact index and the
recent-history journal — and both change while a conversation is running: an
artifact link arrives when something is delivered, a journal entry lands during
consolidation. Either one changing re-billed the entire conversation behind it on
the next turn, for a difference of a few lines, and did it worst on exactly the
long conversations where the bill is largest.

`build_system_prompt_parts()` now returns the prompt in two halves — what
instructs, and the state bands — and `build_messages()` puts the stable half in
the system message and appends the state half to the **tail** of the current
turn's message, next to the runtime-context blocks that already went there. The
system message and every frozen message then form one byte-identical prefix from
turn to turn.

Measured with the real builder: a 120-exchange conversation with one new journal
entry between two consecutive turns went from a **38681/205630 char cacheable
prefix (18.8%)** to **121555/206248 (58.9%)**. Before, only the system prompt
itself was cacheable and the rest of the conversation was re-billed in full; now
the whole unchanged conversation is, and the uncached slack is just the newest
exchange. `build_system_prompt()` still returns the whole prompt joined, so
nothing else on the wire changed.

The archived-context summary stays in the stable half on purpose: compaction
rewrites the older messages, so the prefix is already broken at that moment and
nothing is gained by moving it.

## Configuration

Set it per provider in the **admin page** (`/admin` → *Provider settings* →
*Prompt caching*), or as a default for every provider with
`POWERX_PROMPT_CACHE=auto|markers|off`. Precedence, highest first:

1. the provider's saved `prompt_cache` setting,
2. `POWERX_PROMPT_CACHE`,
3. `POWERX_FORCE_CACHE_MARKERS=1` (legacy alias for `markers`),
4. the provider spec's `supports_prompt_caching` flag (Claude, OpenRouter),
5. `auto`.

`auto` keeps the historical behaviour exactly: markers are still sent for Claude
models on a spec that advertises prompt caching, and nothing is sent for plain
OpenAI-compatible endpoints.

A hard refusal is survivable: if an endpoint rejects a request naming
`cache_control` *or* `prompt_cache_key`, the call is retried once with every
cache field stripped and the markers stay off for that provider instance. The
turn completes uncached instead of failing on a setting that was turned on to
save money. Only a *loud* refusal can be caught this way; the gemini-proxy
rejects nothing and reports nothing, which is why a setting that cannot be
verified from the outside is measured by the admin cache test instead.

## Measuring it

**Admin → Provider settings → Test caching** sends the same large prefix four
times and reports:

* how many prompt tokens the re-warm had cached (`auto` hits),
* whether the endpoint accepted a marked request,
* whether the model actually *received* the marked block (the recital check,
  read **against the unmarked reply** as its baseline — see below),
* whether the endpoint accepted `prompt_cache_key`, on the same body production
  sends it on,
* whether the usage block carries a cache field **at all** — an endpoint that
  reports nothing is not the same as one reporting zero, and the probe says
  which it is instead of claiming a full-price repeat it cannot see,
* the recommended mode, and a warning when markers are unsafe. Findings are
  listed together, not just the first one that applies: the endpoint in use here
  accepts markers, drops the block, and reports no cache, and all three belong
  in the answer.

The two automatic-caching calls send the **same prefix and a different trailing
question**. They used to be byte-identical, which quietly measured the wrong
thing on a gateway that caches whole responses — see the xkiro note below.

Run against the live `gemini-proxy.codebanana.app`: `cacheReported: false`,
markers "accepted" while `prompt_tokens` fell `4195 → 13` (the block really was
dropped), `routingKey.accepted: true`, recommended `auto`.

**In the logs**, every turn now emits the hit rate:

```
COST_METER channel=websocket session=... llm_calls=12 served_by=llm \
  prompt_tokens=652199 cached_tokens=0 cache_hit_pct=0.0 completion_tokens=1094 stop=completed
```

`cached_tokens` is accumulated in the runner from the provider's normalised
usage (`prompt_tokens_details.cached_tokens`, top-level `cached_tokens`, or
`prompt_cache_hit_tokens`), so it works for OpenAI, DeepSeek, Qwen/DashScope,
Moonshot/StepFun and any gateway that reports a cache field. A turn whose
`cache_hit_pct` is `0.0` while `prompt_tokens` is in the hundreds of thousands is
paying full price for a conversation that is not changing between tool calls.

**And when the gateway reports nothing, the meter now says `unreported` instead
of `0.0`:**

```
COST_METER channel=websocket session=... llm_calls=6 served_by=llm   prompt_tokens=unreported cached_tokens=unreported cache_hit_pct=unreported   completion_tokens=unreported stop=completed
```

That distinction was earned: make a streamed request to the configured
gemini-proxy **with** `stream_options.include_usage` and the response ends at
`data: [DONE]` with no usage chunk at all (verified live; the same request
non-streamed reports `prompt_tokens` normally). Real turns stream, so without
this the log printed `cache_hit_pct=0.0` on a gateway that never reported a
cache field — an operator would read "nothing is cached" where the truth is
"nothing is *measurable here*". Zero from a gateway that answers is still
printed as zero; only a missing usage block is called unreported.

## Measured: xkiro (`api.xkiro.com/v1`)

Automatic prefix caching works here, is **reported**, and needs no
`prompt_cache_key`. With `qwen/qwen3.8-omni-flash:free` and a ~5.6k-token system
prefix, three consecutive calls whose bodies differ only in the trailing
question:

```
call 1 (cold prefix)          prompt=5616  prompt_tokens_details: absent
call 2 (same prefix, new ask) prompt=5616  cached_tokens=5376  (95.7%)
call 3 (keyless, same prefix) prompt=5320  cached_tokens=3584  (67.4%)
```

So `auto` is the right mode for it, and the provider parses the hit: on the
streamed path the runner's usage came back `cached_tokens=3584`, and
`COST_METER` printed `cache_hit_pct=67.4` for the warm turn and `0.0` for the
cold one.

Three things were learned the hard way, and all three are now guarded by tests:

**1. This gateway caches whole *responses*, and a replayed body carries the cold
usage block.** Send a byte-identical body twice and the second answer comes back
with the same completion id and the same usage as the first — the pre-warm,
uncached one. The probe used to measure its `auto` hits from exactly that pair,
so on this endpoint it reported "no cache field at all, so a hit cannot be
confirmed from here" about a gateway that reports a 95.7% hit on the very next
distinct request — and, worse, went on to recommend `markers` on the strength of
a keyed call whose body could not be replayed. The fix is one line of intent:
the re-warm varies only the tail question, so the prefix stays byte-identical
while the body cannot be answered from a cache.

**2. A model that refuses to repeat a token is not a gateway that dropped a
block.** The recital check reads the code back out of the marked block, and
`qwen3.8-omni-flash` declines to repeat it at all — on the unmarked request too
(`I cannot provide internal system codes`). Judging markers by the marked reply
alone therefore emitted the probe's loudest line, "Do NOT use markers on this
endpoint", about an endpoint whose markers were fine. The unmarked reply is now
the baseline: markers are only blamed when the unmarked request recited the code
and the marked one did not. When neither recites, the verdict says the check was
inconclusive rather than accusing, and falls back to the one signal that does not
depend on the model's willingness — a *collapsed* `prompt_tokens` (the
gemini-proxy signature, `4234 → 14`), which means the request itself lost the
block.

**3. A cold call omits `prompt_tokens_details` entirely, and the log prints that
as `0.0`.** Call 1 above reported `prompt_tokens=5616` with no cache field, and
`COST_METER` printed `cache_hit_pct=0.0` for it. The `unreported` rule from
`de8f534` only fires when `prompt_tokens` is also missing, so it does not cover
this case: the gateway answered, and the cache breakdown is simply absent. The
normalised usage dict cannot tell "no field" from "field = 0" — by design,
asserted in `test_extract_usage_cached_tokens_zero_should_not_be_included` — so
the meter has no way to distinguish them without a change to that shape. This is
left as a known gap rather than papered over: read a `0.0` on a *cold* turn as
"nothing to cache yet", not as a measurement.
