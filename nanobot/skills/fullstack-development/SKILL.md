---
name: fullstack-development
description: Build complete web applications end-to-end — architecture that ties frontend + backend + database together, project structure, data flow, auth flows, CRUD features, environment configuration, build scripts, and a deploy-ready app for Vercel. Use when the user wants a working web app with UI plus API plus persistence, or to wire existing pieces into one product. Orchestrates frontend-development, backend-development, database-development, web-design, vercel-deployment.
metadata:
  nanobot:
    always: false
---

# Full-Stack Development

Deliver a coherent, deployable web application where the UI, API, and database work as one.
This skill orchestrates the others; consult them for depth.

## Recommended default stack

**Next.js (App Router) + Tailwind + Prisma/Supabase on Vercel.** One repo, one deploy,
server components for reads, route handlers/server actions for writes, env for config.

## End-to-end feature recipe (CRUD example)

1. **Model the data** (`database-development`): define schema + migration.
2. **Build the API** (`backend-development`): Next route handler or server action with validation.
3. **Wire the UI** (`frontend-development`): fetch data, render loading/error/empty, submit forms.
4. **Style it well** (`web-design`): apply tokens/components so it doesn't look generic.
5. **Configure env** (`vercel-deployment`): set `DATABASE_URL`, secrets via `web_dev set_env`.
6. **Deploy & verify** (`vercel-deployment`): `web_dev action=deploy`, open the live URL.

## Project structure (Next full-stack)
```
app/
  layout.tsx            # global shell, fonts, metadata
  page.tsx              # home (server component)
  dashboard/page.tsx    # protected page
  api/<resource>/route.ts   # REST endpoints
components/
  ui/*                  # primitives
  features/*            # domain widgets
lib/
  db.ts                 # prisma/supabase singleton
  auth.ts               # session helpers
prisma/
  schema.prisma
.env.example            # document required vars
```

## Data flow rules

- Reads in server components / route GETs; writes via POST/PATCH/DELETE or server actions.
- Validate on the server even if the client also validates (never trust the browser).
- Keep secrets server-side; only `NEXT_PUBLIC_*` reaches the browser.
- Cache expensive reads (`next.revalidate`); revalidate after writes.

## Auth flow across the stack

- Login form → POST `/api/auth/login` → set httpOnly cookie → redirect to dashboard.
- Dashboard server component calls `requireUser()`; if no session, redirect to login.
- Logout clears the cookie. See `backend-development/references/auth.md`.

## Environment variables checklist

Before deploying, ensure every var the app needs exists in Vercel production:
- `DATABASE_URL` (and `DIRECT_URL` if using Prisma pooler)
- `JWT_SECRET` or auth secret
- third-party keys (`STRIPE_SECRET_KEY`, `OPENAI_API_KEY`, etc.)
Set each with `web_dev action=set_env ... environment=production`, THEN deploy.

## Definition of done

- [ ] App builds locally (`npm run build`) with no errors.
- [ ] All routes return correct status codes; happy path works end-to-end.
- [ ] Loading, error, and empty states present in the UI.
- [ ] Mobile responsive; design follows `web-design` principles.
- [ ] Secrets in env, none hardcoded; public vars prefixed correctly.
- [ ] Deployed to Vercel; live URL tested in a real browser.

## Reference files

- `references/crud-walkthrough.md` — a complete todo-app wiring (schema→API→UI→deploy).