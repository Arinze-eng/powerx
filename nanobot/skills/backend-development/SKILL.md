---
name: backend-development
description: Build web backends and APIs — Node/Express, Next.js route handlers & server actions, REST and GraphQL design, authentication (JWT/sessions/OAuth), input validation, error handling, security headers, CORS, rate limiting, and serverless functions on Vercel. Use when writing any server-side code, API endpoints, auth flows, or connecting a frontend to data. Pairs with database-development and vercel-deployment.
metadata:
  nanobot:
    always: false
---

# Backend Development

Build secure, reliable APIs that run on Vercel's serverless runtime (or any Node host).
Keep handlers thin; put logic in modules; validate everything at the boundary.

## Where the backend runs

| Scenario | Approach |
| --- | --- |
| Full-stack app on Vercel | Next.js App Router `route.ts` handlers + Server Actions |
| Standalone API on Vercel | Express wrapped in a single `server.js` with `vercel.json` routing |
| Lightweight edge logic | Vercel Edge Functions (`runtime = 'edge'`) |

For deployment specifics see `vercel-deployment`. For DB access see `database-development`.

## API design rules

- RESTful resources: `GET /api/users`, `POST /api/users`, `GET /api/users/:id`, `PATCH`, `DELETE`.
- Return consistent JSON shapes; use proper HTTP status codes (200/201/400/401/403/404/500).
- Version under `/api/v1` only if you expect breaking changes.
- Paginate list endpoints (`?limit=&offset=` or cursor).
- Validate every input; never trust the client.

## Handler pattern (Next.js route handler)
```ts
// app/api/todos/route.ts
import { NextResponse } from 'next/server';
import { z } from 'zod';
import { db } from '@/lib/db';

const TodoSchema = z.object({ title: z.string().min(1).max(200) });

export async function GET() {
  const todos = await db.todo.findMany({ orderBy: { createdAt: 'desc' } });
  return NextResponse.json(todos);
}
export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  const parsed = TodoSchema.safeParse(body);
  if (!parsed.success) return NextResponse.json({ error: parsed.error.flatten() }, { status: 400 });
  const todo = await db.todo.create({ data: parsed.data });
  return NextResponse.json(todo, { status: 201 });
}
```

## Express-on-Vercel pattern
```js
// server.js
import express from 'express';
const app = express();
app.use(express.json());
app.get('/api/health', (_req,res)=>res.json({ ok:true }));
app.post('/api/data', (req,res)=>{ /* validate + respond */ });
export default app;   // Vercel calls this as a serverless function
```
`vercel.json`:
```json
{ "version":2, "builds":[{"src":"server.js","use":"@vercel/node"}],
  "routes":[{"src":"/(.*)","dest":"server.js"}] }
```

## Authentication

- Sessions: store an httpOnly cookie with a signed session id; verify on each request.
- JWT: short-lived access token in memory/httpOnly cookie; refresh via secure endpoint.
- OAuth: delegate to provider (GitHub/Google); exchange code for token server-side.
- Never put secrets in client code; read them from env (`process.env.SECRET`).
- Hash passwords with bcrypt/argon2; compare with constant-time helpers.

See `references/auth.md` for concrete JWT + cookie examples.

## Security checklist

- Input validation (schema) on all writes/queries.
- Parameterized queries / ORM — no string SQL concatenation.
- CORS locked to your frontend origin(s).
- Rate-limit public endpoints.
- Set security headers (CSP, X-Content-Type-Options, Referrer-Policy).
- Don't leak stack traces in production responses.
- Verify webhook signatures before trusting payloads.

## Error handling

- Central error mapper returning `{ error: { message, code } }` with correct status.
- Log server-side (don't expose internals to client).
- Wrap async handlers so unhandled rejections become 500s, not crashes.

## Environment variables

Read config from env; declare required vars explicitly and fail fast if missing. Set them
with `web_dev action=set_env` BEFORE deploying. Public (browser) vars must be prefixed
`NEXT_PUBLIC_`; everything else stays server-only.

## Reference files

- `references/auth.md` — JWT + httpOnly cookie auth, password hashing, middleware.
- `references/validation.md` — Zod schemas, sanitization, error shapes.
- `references/security.md` — CORS, headers, rate limiting, webhook verification.