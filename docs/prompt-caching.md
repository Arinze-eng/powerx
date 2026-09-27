# Prompt caching on OpenAI-compatible endpoints

Every tool call in a task re-sends the whole conversation, so the prompt total
for a turn is the *sum* of each iteration's prompt. A 12-call turn measured in
production on 2026-09-27 cost **652,199 prompt tokens** with the completion flat
at ~90 tokens per call — the conversation was being re-sent in full, twelve
times. That is the shape of the bill whether or not anything is cached, so the
only way to know what you actually paid for is to record how much of each
prompt the gateway served from its cache.

## Two mechanisms, not one

| mode | what it does | when it is safe |
| --- | --- | --- |
| `auto` (default) | Nothing is added to the request. The gateway hashes the stable leading bytes (system message, tool schema, history) and bills a repeat against its stored copy. | Always. A hit is visible only in `usage`. |
| `markers` | Adds Anthropic-style `cache_control: {"type": "ephemeral"}` breakpoints to the system message, the second-to-last message and the tail of the tool list. | Only on an endpoint verified to accept them. |
| `off` | Sends nothing cache-related, including no markers for Claude. | When you want the request byte-identical to an uncached one. |

Both mechanisms were probed live before this was written.

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

A hard refusal is survivable: if an endpoint rejects a marked request with a
`cache_control` error, the call is retried once with the markers stripped and
they stay off for that provider instance. The turn completes unmarked instead of
failing on a setting that was turned on to save money.

## Measuring it

**Admin → Provider settings → Test caching** sends the same large prefix three
times and reports:

* how many prompt tokens the second request had cached (`auto` hits),
* whether the endpoint accepted a marked request,
* whether the model actually *received* the marked block (the recital check),
* the recommended mode, and a warning when markers are unsafe.

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
