---
name: browser-automation
description: "Drive a real browser inside the sandbox to browse sites, log in, tap/click, type, scroll, navigate, screenshot, and read interfaces. Use whenever a task needs to interact with a live website or web app (login flows, dashboards, forms, scraping JS-rendered pages) rather than plain HTTP fetch."
metadata: {"nanobot":{"emoji":"🌐","requires":{"bins":["python3"]}}}
---

# Browser Automation in the Sandbox

You drive a real Linux sandbox through the **`novita_sandbox`** tool (actions: `run`, `write`,
`read`, `install`, `upload`, `download_url`, `list`) and the batch runner **`sandbox_batch`**.
Treat it as a desktop you can control with a headless browser. Prefer **Playwright**; fall back to
**Selenium** only if Playwright cannot be installed. Never claim browsing is unsupported —
install what you need first.

> How to call: run shell commands via `novita_sandbox(action="run", command="...")`, write files
> via `novita_sandbox(action="write", path="/workspace/x.py", content="...")`, then execute them.
> For multi-step flows use `sandbox_batch` so intermediate output stays in the sandbox.

## Golden rules

1. **Prefer Playwright over Selenium.** Playwright bundles its own browser + driver, handles
   waits, and is far more reliable for scripted interaction. Use `playwright` (Python).
2. **Install once, reuse.** Check before installing; the sandbox may already have deps.
3. **Always wait for the page, never `sleep(N)` blindly.** Use explicit waits / auto-waiting.
4. **Screenshot to *see*, then act.** Capture the screen, read it with vision, decide the next
   selector. This is how you understand unfamiliar interfaces.
5. **Verify every action landed.** After a click/login/submit, assert the expected change
   (URL, element text, new content) before continuing.
6. **Never hardcode secrets.** Read credentials from env vars / config passed by the user; do
   not paste passwords into logs or replies.

## Setup (idempotent — run via `novita_sandbox(action="run")`)

```bash
# Detect existing install first
python3 -c "import playwright" 2>/dev/null && echo HAVE_PW || echo NO_PW

# Install Playwright + Chromium (headless shell) and system deps
pip install --quiet playwright
playwright install --with-deps chromium
```

If `pip` is blocked by an externally-managed environment, use one of:
```bash
pip install --break-system-packages playwright   # Debian/Ubuntu PEP668
python3 -m venv /tmp/bvenv && /tmp/bvenv/bin/pip install playwright && /tmp/bvenv/bin/playwright install --with-deps chromium
apt-get update && apt-get install -y python3-pip   # if pip itself is missing
```
Only escalate to `sudo`/`apt-get` when genuinely required. If `apt-get` fails due to a
read-only rootfs, that is expected on hardened sandboxes — rely on `pip`/`venv`, and report
the specific blocker instead of giving up.

### Selenium fallback (only if Playwright is impossible)
```bash
pip install selenium webdriver-manager
# chromedriver must match Chrome version; webdriver-manager resolves it
```

## Minimal working pattern (Playwright, sync API)

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )
    page = ctx.new_page()
    page.goto("https://example.com", wait_until="domcontentloaded")
    page.screenshot(path="/workspace/page.png", full_page=True)
    browser.close()
```
Save screenshots under `/workspace/` so they are downloadable artifacts, then deliver them.

## Core interactions — the verbs you were asked to teach

| Task | Playwright call | Notes |
|---|---|---|
| Navigate | `page.goto(url)` / `page.click(a[href])` | prefer role/text selectors after nav |
| Wait | `page.wait_for_selector(css)`, `expect(locator).to_be_visible()` | NEVER bare `sleep` |
| Click / Tap | `page.get_by_role("button", name="Submit").click()` | role-based beats brittle CSS |
| Type | `page.fill("#email", value)` | clears then types; use `.type()` for keystrokes |
| Scroll | `page.mouse.wheel(0, 800)` or `el.scroll_into_view_if_needed()` | loop to reach lazy content |
| Screenshot | `page.screenshot(path=..., full_page=True)` | full_page captures below-fold |
| Read DOM | `page.inner_text(sel)`, `page.content()` | extract data after render |
| Login | fill user/pass → click submit → `wait_for_url("**/dashboard")` | verify success explicitly |
| Handle dialogs | `page.on("dialog", lambda d: d.accept())` | alerts/confirmations |
| New tab/window | `ctx.expect_page()` around a click | popups |

### Login flow example
```python
page.goto("https://site.example/login")
page.fill('input[name="email"]', os.environ["SITE_EMAIL"])
page.fill('input[name="password"]', os.environ["SITE_PASSWORD"])
page.get_by_role("button", name=re.compile("log ?in", re.I)).click()
page.wait_for_url(re.compile(r"/(dashboard|home|app)"), timeout=15000)
assert page.locator("text=Welcome").is_visible(), "login did not succeed"
```

## How to understand an unfamiliar interface (be smart)

Do this loop instead of guessing selectors blindly:

1. **Load & screenshot** the page (`full_page=True`).
2. **Read it visually** — identify headings, buttons, inputs, menus, tables, pagination.
3. **Dump structure** when needed:
   ```python
   print(page.evaluate("""() => [...document.querySelectorAll(
     'a,button,input,select,[role],h1,h2,nav,table')].slice(0,80).map(e => ({
       tag:e.tagName.toLowerCase(), role:e.getAttribute('role'),
       text:(e.innerText||e.value||'').trim().slice(0,60),
       href:e.href||null, id:e.id||null, name:e.name||null }))"""))
   ```
4. **Pick robust selectors**: prefer `get_by_role` / `get_by_label` / `get_by_text` / stable
   `data-testid` over generated class names. Avoid absolute XPath.
5. **Act → observe → adapt**: after each action, re-screenshot or re-read to confirm state
   changed as expected; if not, try another selector/strategy.

## Scrolling & infinite feeds
```python
for _ in range(20):
    prev = page.evaluate("document.body.scrollHeight")
    page.mouse.wheel(0, 1200)
    page.wait_for_timeout(400)          # let lazy content load
    if page.evaluate("document.body.scrollHeight") == prev:
        break                            # reached bottom
```

## Waiting strategies (avoid flakiness)
- `wait_until="networkidle"` for SPA-heavy pages (but add a timeout; some pages never idle).
- `page.wait_for_selector(sel, state="visible")` before interacting.
- `expect(locator).to_have_text(...)` for assertions.
- Set generous timeouts (`page.set_default_timeout(30000)`) on slow sites.

## Batch many steps cheaply
Wrap multi-step flows in one `sandbox_batch` op so intermediate output stays in the sandbox and
you only pull back the final screenshot(s)/extracted JSON — this keeps context small and cuts
round-trips. Write the script to a file with `novita_sandbox(action="write", ...)`, then run it,
then read results.

## Troubleshooting
- **Browser won't launch / missing libs:** rerun `playwright install --with-deps chromium`; if
  `--with-deps` needs root and is blocked, install the named shared libraries individually.
- **Timeouts:** increase default timeout, switch `wait_until` to `domcontentloaded`.
- **Cloudflare/bot walls:** set a realistic UA + viewport, `page.wait_for_load_state`, retry
  once; if still blocked, tell the user the site blocks automation rather than looping.
- **Empty screenshots:** ensure `wait_for_load_state("networkidle")` or a selector wait first.
- **Headless detection:** some sites block headless; try `chromium.launch(channel="chrome")`
  or `headless=False` with `xvfb-run` if a virtual display is available.

## Deliverables
Return concrete artifacts: screenshot paths under `/workspace/`, extracted data as JSON/markdown,
and a short statement of what was verified (e.g., "logged in successfully, dashboard visible").
