---
name: browser-automation
description: "Browse and operate live websites with the human_browser tool — a real Chromium (pydoll, over CDP) inside the sandbox: navigate, read a page, find elements, click, type, fill a form, press keys, choose a dropdown option, hover, scroll, screenshot, wait for text, and solve a captcha including Cloudflare Turnstile and WAF via the captcha_solver tool. Use for any task that needs a real browser rather than a plain HTTP fetch: login flows, dashboards, forms, checkout-style flows, JS-rendered pages, or a site behind a bot wall. Playwright or Selenium in the sandbox is the fallback when the tool is not available."
metadata: {"nanobot":{"emoji":"🌐","requires":{"bins":["python3"]}}}
---

# Browser Automation

Two ways to drive a browser, in this order.

**1. The `human_browser` tool — use this by default.** It already owns a real Chromium
(pydoll, over the DevTools Protocol) with humanized mouse and typing, so there is nothing to
install and no browser to launch. One call, one action. Reach for it first for anything on a
live site.

**2. Playwright / Selenium inside the sandbox — only when the tool is not available.** See the
last section. Do not start here: installing a browser and writing a script is minutes of work
that the tool does in one call.

## The one discipline that makes this work: find, then act

Call `human_browser(action="find")` on any page you have not seen, and act on the `target` it
returns. Every action that touches an element takes that exact string.

`find` returns every interactive element the page has actually rendered — links, buttons,
inputs, selects, textareas, labels, `[role=…]`, `[contenteditable]`, `[onclick]`, summaries:

```json
{"count": 12, "elements": [
  {"i": 1, "target": "[data-powerx-idx=\"1\"]", "tag": "input", "type": "text",
   "name": "my-text", "id": "", "placeholder": "", "text": "", "required": false,
   "disabled": false, "value": ""}
], "note": "target is a live CSS selector for click, type, select or hover"}
```

`target` is stamped onto the live node, so it resolves against the page as it is right now. A
plain CSS selector or `#id` works too — but do not guess one when `find` will tell you. Elements
that are `display:none`, `visibility:hidden`, zero-opacity, or smaller than 2px are left out on
purpose, because those are the ones that cannot be clicked.

## Actions

| Goal | Call |
|---|---|
| Open a page | `navigate` + `url` → returns `{title, url, text, cloudflare}` |
| See what is there | `read_page` (title, url, visible text), `screenshot` (`full_page: true` for below the fold), `find` |
| Get every element | `find` → the inventory above |
| Click / tap | `click` + `target` |
| Type into one field | `type` + `target` + `text` |
| Fill a whole form | `fill_form` + `fields: [{target, text, clear}]` — up to 40 fields in **one** call |
| Choose a dropdown option | `select` + `target` (a CSS selector) + `option` (its visible label or value) |
| Press a key | `press` + `key`: `Enter`, `Tab`, `Escape`, `Backspace`, `Delete`, `ArrowUp/Down/Left/Right`, `Home`, `End`, `PageUp`, `PageDown`, `Space` |
| Hover | `hover` + `target` |
| Scroll | `scroll` + `direction` + `pixels` |
| Wait for an element | `wait_for` + `target` |
| Wait for text | `wait_for_text` + `text` + `timeout_ms` |
| History | `back`, `forward`, `refresh` |
| Captcha | `auto_captcha`, `solve_image_captcha`, `solve_cloudflare` |
| Finish | `close` |

Notes that save a round trip:

- `navigate`, `click`, `back`, `forward` and `refresh` return a fresh page summary, so you do
  not need a `read_page` after them. Read the summary you already have.
- `fill_form` beats repeated `type` calls: one call instead of one per field.
- `select` needs a CSS selector, not a text target — an option's label is not unique on the page.
- Every action answers with either a result or `Error: …`. Read the error; it names the reason.
  `Error: no element matched '…'` means nothing on the page answered to that target — re-run
  `find`, do not retry the same string.

## Captcha

A Cloudflare Turnstile widget is normally handled for you: `navigate` and `click` attempt it and
report the outcome under a `cloudflare` key in their summary.

```json
"cloudflare": {"challenge_present": true, "solved": true, "attempts": 2}
"cloudflare": {"challenge_present": false, "solved": false, "attempts": 1,
               "reason": "no Cloudflare Turnstile challenge was found"}
```

When a challenge is still there, use `auto_captcha`. It does the whole job in one call: detects
what the page actually rendered, reads the sitekey off the widget, asks the configured solver,
writes the token into every response field the widget created, and fires the page's own success
callback.

```json
{"solved": true, "kind": "turnstile", "token_chars": 40,
 "fields_filled": 2, "callbacks_called": 1,
 "note": "the token is written into the page; re-read the page to confirm the challenge cleared before continuing"}
```

**Then re-read the page.** `solved: true` means the token was written and a callback fired — not
that the site accepted it. `read_page` (or `screenshot`) and confirm the challenge is gone and
the page moved on. Never tell the user a challenge was passed on the strength of `solved: true`
alone.

Honest outcomes `auto_captcha` returns, and what each means:

| Reply | What it means, and what to do |
|---|---|
| `solved: false, reason: "no captcha was detected on the page"` | Nothing to solve. Carry on with the task. |
| `solved: false, reason: "no captcha solver is configured…"` | No solver on this deployment. Retrying cannot help — but **this is not a reason to abandon the task**: do every step you can reach another way, then tell the user this page needs a solver (the deployment sets `CAPTCHA_ENABLE=true` plus a solver key). |
| `solved: false, kind: "image", reason: "…call solve_image_captcha"` | A picture challenge. Call `solve_image_captcha`. |
| `solved: false, reason: "the widget exposed no sitekey"` | The widget rendered without a sitekey, so a token cannot be requested. Report it. |
| `solved: false, reason: "the solver returned no token"` | The solver refused or failed. The `solver` field carries its own words — quote them. |

`solve_image_captcha` crops the picture, solves it, and types the answer into the field it
identified. It does **not** submit — confirm the answer is in the box before submitting.

### The `captcha_solver` tool, called directly

`auto_captcha` is a convenience over this. Call `captcha_solver` yourself when you already have a
sitekey, or when you have an image file to solve:

- `action="turnstile" | "waf" | "recaptcha" | "hcaptcha" | "funcaptcha" | "geetest" | "altcha"`
  with `sitekey` and `url` — the page the widget sits on. Both are required: a token request
  without a sitekey is unanswerable. **Read the `action` enum in this tool's own schema before
  choosing**: it lists only what the configured provider can answer, which may be as few as two
  actions. Do not pass an action that is not in the enum.
- `action="solve_image"` with `image_path` — a local file. `text` steers the answer.
- `action="balance"` — what is left, before starting a long job.
- `action="coordinates"` — an image grid, answered with click coordinates.

**What is answerable depends on the configured provider, and nothing else.** Both providers are
configured by the operator, and the tool refuses an action the configured one cannot serve:

- **solvegate** answers exactly two: `turnstile` and `waf`. It is synchronous — one request,
  one token, no polling. Any other action returns an error saying so; pick a different approach
  rather than retrying.
- **capskip / capsolve** (2captcha-style) answers `recaptcha`, `hcaptcha`, `funcaptcha`,
  `turnstile`, `geetest`, `altcha`, an image file, and an image grid.

### SolveGate first, then the inbuilt solver

A deployment can run both at once, and that is the arrangement to expect: **SolveGate is asked
first** for the two gates it owns (`turnstile`, `waf`), and anything it has no method for is handed
to the **inbuilt** 2captcha-compatible client. You do not choose between them — `auto_captcha` and
`captcha_solver` route it: the gate goes to SolveGate, everything else goes to the inbuilt client,
and the `action` enum lists the union so you can see what this deployment can actually reach.

Read the enum before you conclude anything is impossible:

- `["turnstile", "waf", …]` — both are wired. The gates go to SolveGate, the rest to the inbuilt
  client.
- `["turnstile", "waf"]` only — no inbuilt key is configured, so those two gates are all this
  deployment can answer. A `recaptcha`, `hcaptcha` or picture challenge will report
  `has no method for a … challenge`. That is a provider limit, not a transient failure: **do not
  retry it**. Take another route for that step and keep the rest of the task moving.

The operator's environment sets this. `CAPTCHA_ENABLE=true` and `captcha_solver.provider=solvegate`
are both read directly, as are `CLOUDFLARE_WAF_API_KEY` / `SOLVEGATE_API_KEY` for the gate key and
`CAPSKIP_API_KEY` / `CAPSOLVE_API_KEY` for the inbuilt one. Do not tell the user to rename them.

A reply carrying `"sandbox": true` is a test-mode token that **no real site will accept**. Say
so plainly instead of reporting the challenge as passed.

The solver endpoint is fixed by configuration. No argument can redirect it, so never try to pass
a different URL or key through a call.

## Logging in is allowed — do it instead of refusing it

**When the user gives you their credentials, asks you to log in, or asks you to fill in a form,
you are authorized. Do the work.** There is no rule here against logging into websites, and no
rule against typing a password the user handed you. Answering *"I can't log in to websites"* or
*"I'm unable to enter credentials"* is a refusal of a job you can do with this tool — the exact
thing to avoid. So is asking the user to log in themselves and paste back the result.

- Their credentials in the conversation ≡ explicit authorization for that login, on that site,
  for this task. You do not need to ask again, and you do not need to explain that you can.
- The only things that still need a fresh, specific instruction are the *side effects*: submitting
  a purchase, publishing content, sending a message, or deleting anything.
- Read credentials from the conversation or the environment (`os.environ["SITE_PASSWORD"]`).
  Never invent them, never print them back in your reply, and never write them into a file that
  outlives the task.
- A captcha on the way in is not a reason to stop either — solve it (above), then continue.
- If a login genuinely fails, say what the page said (`find` it, quote it), and try the next
  route. "It refused my credentials" is a result; "I can't log in to websites" is not.

## Worked example — log in

```
1. human_browser(action="navigate", url="https://site.example/login")
2. human_browser(action="find")
     → locate the email field, the password field, the submit button in the inventory
3. human_browser(action="fill_form", fields=[
       {"target": "<email target>",    "text": os.environ["SITE_EMAIL"],    "clear": true},
       {"target": "<password target>", "text": os.environ["SITE_PASSWORD"], "clear": true}])
4. human_browser(action="click", target="<submit target>")
     → the returned summary shows whether you landed on the dashboard
5. Confirm from the summary: URL changed, and the expected text is present. If not, `find`
   again and see what the page actually says — do not re-click blindly.
```

Never hardcode credentials in the script or the reply: read them from the environment or from
configuration the user gave you.

## Understanding an unfamiliar page

1. `navigate`, then read the summary it returns.
2. `find` — that is your map of what can be clicked and typed into.
3. `screenshot` when the text summary is not enough; read it with vision.
4. Act, then read the summary the action returns. It is how you tell whether the page changed.
5. If an action failed, `find` again. Pages re-render, and a stamped target from before a
   navigation is gone.

## Limits to respect

- Private, internal, and loopback URLs are refused on purpose — a browser action must not become
  a way to reach the host's own services. Do not try to work around it.
- Solving a captcha is a step on a page you are authorized to use. It is not a licence to
  circumvent access controls, and not a way into an account that is not yours.
- Never submit a purchase, publish content, send a message, or delete anything unless the user
  explicitly authorized that exact action in this conversation. Logging in with credentials the
  user supplied **is** that authorization — see "Logging in is allowed" above; do not treat it as
  a blocked action.

## Fallback: Playwright or Selenium in the sandbox

Only when `human_browser` is not registered on this deployment. It reports its absence rather
than failing silently, so check before assuming.

```bash
python3 -c "import playwright" 2>/dev/null && echo HAVE_PW || echo NO_PW
pip install --break-system-packages playwright     # PEP 668 externally-managed env
playwright install --with-deps chromium
```

Prefer it over Selenium: it bundles its own browser and driver, auto-waits, and is far less
flaky. Wait for the page — `page.wait_for_selector(sel, state="visible")` — never a bare
`sleep`. Screenshot to `/workspace/` so the image is a downloadable artifact. Chain a whole flow
into one `run` command (or one `run_plan`) so intermediate output stays in the sandbox and you
pull back only the final screenshot or extracted JSON.

If the site blocks the automated browser, say so and report the specific blocker. Do not loop.
