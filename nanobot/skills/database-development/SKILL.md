---
name: database-development
description: Design and use databases for web apps — relational schema design (Postgres/MySQL), SQL queries, migrations, indexes, ORMs (Prisma, Drizzle, Supabase client), connection pooling for serverless, seed data, and storing DB credentials as env vars. Use when modeling data, writing queries/migrations, connecting to Postgres or Supabase, or making a full-stack app persistent. Pairs with backend-development and vercel-deployment.
metadata:
  nanobot:
    always: false
---

# Database Development

Model data cleanly, query safely, and connect reliably from serverless functions on Vercel.

## Choosing storage

| Need | Pick |
| --- | --- |
| Relational data, many relations | Postgres (Neon / Supabase) + Prisma or Drizzle |
| Quick auth + tables + realtime | Supabase (Postgres + client SDK + RLS) |
| Simple key/value / cache | Upstash Redis |
| Object/file storage | Vercel Blob / S3-compatible |

For this project, prefer **Supabase Postgres** or **Neon**, both work great with Vercel.

## Schema design rules

- Normalize to 3NF unless you have a measured reason to denormalize.
- Every table has an `id` primary key (uuid or bigint identity).
- Add `created_at`/`updated_at` timestamps (default now()).
- Name things consistently: snake_case columns, plural table names.
- Index foreign keys and any column used in WHERE/ORDER BY/joins.
- Enforce integrity with NOT NULL, UNIQUE, FK constraints — don't rely on app code alone.
- Soft-delete (`deleted_at`) only if you need audit trails.

Example (SQL):
```sql
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext UNIQUE NOT NULL,
  name text NOT NULL,
  password_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE posts (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title text NOT NULL,
  body text,
  published boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX posts_user_idx ON posts(user_id);
```

## Migrations

- Always use migration files (never hand-run prod DDL).
- Prisma: `npx prisma migrate dev --name init`.
- Drizzle: `npx drizzle-kit generate && npx drizzle-kit push`.
- Keep each migration reversible-ish; test on a branch/staging DB first.

See `references/prisma.md` and `references/supabase.md`.

## Serverless connection strategy

Serverless functions spin up/cold-start often, so naive per-request connections exhaust the
DB's max connections. Fix it:
- Use a **connection pooler** (PgBouncer via Supabase/Neon, transaction mode).
- Reuse the client across invocations by caching it on a module/global singleton.
- Set `pool_timeout`, `connect_timeout`, and keep idle clients short-lived.

```ts
// lib/db.ts (Prisma singleton pattern for Vercel)
import { PrismaClient } from '@prisma/client';
const g = globalThis as unknown as { prisma?: PrismaClient };
export const db = g.prisma ?? new PrismaClient();
if (process.env.NODE_ENV !== 'production') g.prisma = db;
```

## Credentials & env

Store the DB URL in env, set BEFORE deploy with `web_dev action=set_env`:
- `DATABASE_URL=postgresql://user:pass@host:5432/db?pgbouncer=true&connection_limit=1`
- For Supabase: `SUPABASE_URL`, `SUPABASE_ANON_KEY` (public), `SUPABASE_SERVICE_ROLE_KEY` (server-only).

Never log full connection strings. Anon key is public; service-role key is secret.

## Query safety

- Parameterized queries / ORM only. No string interpolation of user input into SQL.
- Validate pagination limits server-side.
- Use transactions for multi-step writes.
- Select only needed columns; avoid `SELECT *` on wide tables.

## Reference files

- `references/prisma.md` — schema, migrations, seeding, singleton client.
- `references/supabase.md` — Supabase client, RLS policies, auth integration.