# Prisma on Vercel

## schema.prisma
```prisma
generator client { provider = "prisma-client-js" }
datasource db { provider = "postgresql"; url = env("DATABASE_URL") }

model User {
  id        String   @id @default(uuid())
  email     String   @unique
  name      String
  passwordHash String @map("password_hash")
  posts     Post[]
  createdAt DateTime @default(now()) @map("created_at")
}

model Post {
  id        Int      @id @default(autoincrement())
  userId    String   @map("user_id")
  user      User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  title     String
  body      String?
  published Boolean  @default(false)
  createdAt DateTime @default(now()) @map("created_at")
  @@index([userId])
}
```

## Commands
```bash
npx prisma migrate dev --name init   # create + apply migration locally
npx prisma generate                  # build client (run in CI/build too)
npx prisma db seed                   # seed test data
```

## Singleton client (`lib/db.ts`)
```ts
import { PrismaClient } from '@prisma/client';
const g = globalThis as unknown as { prisma?: PrismaClient };
export const db = g.prisma ?? new PrismaClient();
if (process.env.NODE_ENV !== 'production') g.prisma = db;
```

## Usage
```ts
const users = await db.user.findMany({ where:{ email }, include:{ posts:true } });
await db.post.create({ data:{ userId, title } });
```

## Env for deploy
Set `DATABASE_URL` with `web_dev action=set_env`. Add a `postinstall` script so the
client is generated during the Vercel build:
```json
{ "scripts": { "postinstall": "prisma generate", "build": "next build" } }
```

## Pooling note
For serverless use Neon/Supabase pooler and append `?pgbouncer=true&connection_limit=1`
(or set via `directUrl`). Keep `pool_timeout` low.