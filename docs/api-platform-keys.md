# API platform keys

Issue an OpenAI-compatible API key for a running nanobot and call it from any
client. The same key store backs the Telegram bot's `/apikey` command and the web
settings pane, so keys are interchangeable between the two.

## Generating a key

**Settings → API platform → Generate**, optionally naming the key first.

The generated key is displayed **once**, in a green panel at the top of the
section. The server stores only a SHA-256 hash, so the plaintext cannot be
retrieved afterwards — the key list shows a prefix (`px_ab12…`) for identification
only.

When a key is generated the panel:

1. **scrolls into view**, and
2. **copies the key to the clipboard automatically**, showing
   *"Copied to your clipboard automatically"* under it.

If the browser refuses the clipboard write, the panel says so in red, selects the
whole key, and tells you to copy it manually (Ctrl/Cmd-C, or long-press → Copy on
a phone). A clipboard write that silently does nothing is the failure this exists
to avoid: the button would look like it worked while the one-time key was gone for
good.

**If you dismiss the panel without storing the key there is no recovery.** Revoke
and generate a new one.

### On phones and in WebViews

The key is shown *wrapped rather than truncated*, and tapping it selects the whole
value. A key you cannot see the end of is a key you cannot type out when the
clipboard is unavailable, which on a WebView is a normal state rather than an
error.

Clipboard support, in order of preference:

| Environment | Path |
|---|---|
| Secure context (HTTPS / localhost) | `navigator.clipboard.writeText` |
| Insecure context, older WebView | `document.execCommand("copy")` on a hidden textarea |
| Both refused | Panel reports failure, selects the key, asks for a manual copy |

The legacy path is attempted **synchronously**, before any `await`. `execCommand`
requires transient user activation, and awaiting spends it — which is why an
earlier version of this helper worked on desktop and silently failed on exactly
the Android WebViews that needed it.

## Using a key

```bash
curl "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer px_..." \
  -H "Content-Type: application/json" \
  -d '{"model":"nanobot","messages":[{"role":"user","content":"hello"}]}'
```

`GET $BASE_URL/v1/models` lists what the deployment exposes. Anything that accepts
an OpenAI base URL works — the SDKs, LangChain, Continue, LobeChat, and so on. The
Base URL and the chat-completions endpoint are both shown in Settings, each with
its own copy button.

Keys created in the web app work through the Telegram bot's documented endpoints
and vice versa: one store, one authentication path.

## Limits and lifecycle

- **Maximum active keys per account:** see `max_keys` in the settings payload.
  Generating past the limit returns an error naming the limit and pointing at
  revoke.
- **Revoke a single key** from its row; **Revoke all keys** invalidates every key
  on the account, including ones created in Telegram, immediately. That action is
  deliberately separated from the rest of the panel and asks for confirmation.
- Revoked rows are kept so usage history stays attributable; they render muted and
  cannot authenticate.

## Security

Treat an API key as a password. It carries whatever the account can spend —
including provider credits.

- Keys are shown once and stored hashed. Nothing in the server can print one back.
- Scope names to purpose (`"backend"`, `"notebook"`) so a revoke is targeted.
- Rotate by revoking and regenerating if a key may have leaked.
- Never commit a key. Inject it from the environment.

### Server-side configuration

The API platform reads its credentials from the environment. Two variables in
particular must never have a literal fallback in source:

| Variable | Blast radius if leaked |
|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | Bypasses row-level security. Full read/write on the database, all users. |
| `NOVITA_API_KEY` | Sandboxes created and billed on your account. |

> **Incident note.** `nanobot/agent/powerx_engine.py` previously carried a
> Supabase **service-role key as the default value** of its `os.environ.get` call,
> which put that credential in a public repository's git history. The default is
> now empty, and `supabase_configured()` lets callers fail loudly instead of
> authenticating with a credential that is printed in a source file.
>
> **Removing the string does not un-leak it.** The key must be rotated in the
> Supabase dashboard; it has been in git history and may already have been
> fetched. Rewriting history is worth doing for tidiness and is not a fix. If the
> deployment is genuinely public, audit the database for access you did not expect.

An `os.environ.get("SOME_SECRET", "<literal>")` default looks harmless and is the
easiest way to leak a credential by accident. Use an empty default and a
configuration check.
