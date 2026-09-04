---
name: web-development
description: "Hub for building and deploying web applications end-to-end with the web_dev tool. Use whenever the user asks to create/build/modify a website, web app, landing page, dashboard, API, or full-stack product — especially if they want it deployed and a link returned. Routes to specialized skills - web-design (look & feel), frontend-development (UI code), backend-development (API/auth/security), database-development (data/persistence), fullstack-development (wiring it together), vercel-deployment (ship + env + live URL)."
metadata:
  nanobot:
    always: false
---

# Web Development (hub)

This is the entry point for all web-building work. The actual depth lives in the
specialized skills below — load whichever apply to the task; you don't need them all.

## Route by what the user wants

| User intent | Load skill(s) |
| --- | --- |
| "make it look good / design / UI" | `web-design` |
| "build the pages / React / Next.js / Tailwind" | `frontend-development` (+ `web-design`) |
| "add an API / auth / server logic" | `backend-development` |
| "store data / database / users / persistence" | `database-development` |
| "a whole working app (UI + API + DB)" | `fullstack-development` |
| "deploy it / give me a link / set env vars" | `vercel-deployment` |

## The web_dev tool (what actually ships code)

The agent uses the `web_dev` tool to scaffold and deploy to Vercel:
- `action=scaffold` — starter project (`project`, `type=frontend|backend|fullstack`).
- `action=deploy` — ship a directory, get back the live `https://…vercel.app` URL.
- `action=set_env` — add an env var (`name`,`value`,`environment`) BEFORE deploying.
- `action=status` / `action=inspect` — inspect deployments and env.

Auth uses the operator's `VERCEL_TOKEN`. If the tool reports disabled, tell the user an
admin must configure `VERCEL_TOKEN` on the backend.

## Standard workflow (every web build)

1. **Clarify scope** — what kind of app, does it need a backend/database, any design vibe?
2. **Scaffold or write files** — use `web_dev scaffold` or build directly with filesystem tools.
3. **Design well** — follow `web-design`; never leave default unstyled markup.
4. **Implement** — `frontend-development` / `backend-development` / `database-development`.
5. **Set env** — `web_dev action=set_env` for every secret/config the app needs.
6. **Deploy** — `web_dev action=deploy`; capture the returned URL.
7. **Deliver** — give the user the live URL and confirm it loads.

## Quality bar

- Builds cleanly before deploy (`npm run build`).
- Looks intentional (design tokens, hierarchy, responsive) — not generic AI output.
- Handles loading/error/empty states.
- Secrets in env, none hardcoded; public vars prefixed correctly.
- Tested at mobile widths.

See the linked skills for deep guidance; this file only coordinates them.