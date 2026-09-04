---
name: vercel-deployment
description: Deploy web projects to Vercel using the Vercel CLI via the web_dev tool — create/link projects, set environment variables, choose preview vs production deployments, configure framework/build settings (vercel.json), manage custom domains, view build logs, roll back, and return the live URL to the user. Use whenever the user asks to deploy, host, publish, go live, ship to Vercel, or share a link for a web project. Covers static sites, Next.js, React/Vite SPAs, and Node/Express APIs.
metadata:
  nanobot:
    always: false
requires:
  env: [VERCEL_TOKEN]
---

# Vercel Deployment

Ship any web project to Vercel non-interactively with the `web_dev` tool (which wraps the
Vercel CLI). Authentication uses the operator-configured `VERCEL_TOKEN`; if the tool is
unavailable, tell the user an admin must set `VERCEL_TOKEN`.

## The golden path

1. Build the app locally (see `frontend-development` / `backend-development`).
2. Ensure it builds cleanly (`npm run build`) before deploying.
3. Set required env vars FIRST: `web_dev action=set_env name=... value=... environment=production`.
4. Deploy: `web_dev action=deploy project=<dir>` → returns the live `https://…vercel.app` URL.
5. Give the user the URL and open it to verify.

## web_dev actions (the tool you call)

| Action | What it does | Key args |
| --- | --- | --- |
| `scaffold` | Create a starter frontend/backend/fullstack project | `project`, `type` |
| `deploy` | Deploy a directory to Vercel, return live URL | `project`, `yes`, `timeout` |
| `set_env` | Add an env var to the project | `project`, `name`, `value`, `environment` |
| `status` | List deployments + env vars | `project` |
| `inspect` | Show current deployment/project URLs | `project` |

Always pass `project` as the directory that contains the code you built.

## Preview vs production

- `web_dev action=deploy` defaults to a **production** deployment (live at `<project>.vercel.app`).
- To get a preview-style URL without promoting, deploy from a branch; the CLI prints both URLs.
- Re-running `deploy` after changes creates a new deployment; the previous one stays until superseded. Roll back by deploying an older commit/dir.

## Framework detection & vercel.json

Vercel auto-detects most frameworks. Provide `vercel.json` only when you need overrides:

- **Static site** (plain HTML): no config needed; just deploy the folder with `index.html`.
- **Next.js**: detected automatically; ensure `build` script exists.
- **Vite/React SPA**: set output dir — see below.
- **Node/Express API**: route all paths to your server (see `references/config.md`).

```json
// Vite SPA -> serve dist/, redirect unknown routes to index.html
{ "buildCommand": "npm run build", "outputDirectory": "dist",
  "rewrites": [{ "source": "/(.*)", "destination": "/index.html" }] }
```

## Environment variables (critical order)

Set env BEFORE deploying so the build can read them. Production runs on Vercel's servers,
not your machine, so anything the app needs must exist in Vercel:
- Secrets: `DATABASE_URL`, `JWT_SECRET`, `STRIPE_SECRET_KEY`, etc. → `environment=production`.
- Public browser vars: prefix `NEXT_PUBLIC_` (or `VITE_` for Vite) so they're exposed safely.
- After changing env, redeploy to apply it to the running deployment.

Use `web_dev action=status` to confirm what's set. Sensitive values are masked by Vercel.

## Custom domains

If the user wants a real domain (e.g. `app.example.com`):
1. In Vercel dashboard → Project → Settings → Domains, add the domain (or `vercel domains add <domain>`).
2. Point DNS at Vercel (A/CNAME records shown in the dashboard).
3. The domain becomes an alias for the production deployment automatically.

For this agent, recommend the user add domains via the Vercel dashboard since DNS changes
are outside the sandbox; the `*.vercel.app` URL works immediately after deploy.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Build fails | Run `npm run build` locally; fix errors; check Node version in `package.json` engines. |
| Missing env at runtime | Set it with `set_env`, then redeploy. |
| Blank page on SPA refresh | Add `rewrites` to `vercel.json` (SPA fallback). |
| API 404 | Ensure route handler path matches (`/api/...`) or Express catch-all route. |
| Cold-start DB timeouts | Use a pooler connection string (see `database-development`). |
| Tool disabled | Operator must set `VERCEL_TOKEN` on the backend. |

View logs: `vercel logs <deployment-url> --token $VERCEL_TOKEN` (run inside sandbox/exec).

## Reference files

- `references/cli.md` — full Vercel CLI command list with token auth.
- `references/config.md` — vercel.json recipes per framework.
- `references/env-vars.md` — env var strategy, public vs secret, examples.