# Full-stack CRUD walkthrough (Next.js + Prisma)

Build a "notes" app: list, create, edit, delete notes with persistence.

## 1. Database (`prisma/schema.prisma`)
```prisma
model Note {
  id        String   @id @default(uuid())
  title     String
  body      String?
  createdAt DateTime @default(now()) @map("created_at")
}
```
Migrate: `npx prisma migrate dev --name add_notes`.

## 2. DB client (`lib/db.ts`) — singleton for Vercel
```ts
import { PrismaClient } from '@prisma/client';
const g = globalThis as unknown as { prisma?: PrismaClient };
export const db = g.prisma ?? new PrismaClient();
if (process.env.NODE_ENV !== 'production') g.prisma = db;
```

## 3. API routes (`app/api/notes/route.ts`)
```ts
import { NextResponse } from 'next/server';
import { z } from 'zod';
import { db } from '@/lib/db';

const CreateNote = z.object({ title: z.string().min(1).max(120), body: z.string().max(5000).optional() });

export async function GET() {
  return NextResponse.json(await db.note.findMany({ orderBy:{ createdAt:'desc' } }));
}
export async function POST(req: Request) {
  const parsed = CreateNote.safeParse(await req.json().catch(()=>null));
  if (!parsed.success) return NextResponse.json({ error: parsed.error.flatten() }, { status:400 });
  const note = await db.note.create({ data: parsed.data });
  return NextResponse.json(note, { status:201 });
}
```
`app/api/notes/[id]/route.ts`:
```ts
import { NextResponse } from 'next/server';
import { db } from '@/lib/db';
export async function DELETE(_req:Request,{ params }:{params:{id:string}}){
  await db.note.delete({ where:{ id:params.id } });
  return NextResponse.json({ ok:true });
}
```

## 4. UI (`app/page.tsx`) — server component fetches notes
```tsx
import { db } from '@/lib/db';
import { NewNoteForm } from '@/components/NewNoteForm';
export default async function Page(){
  const notes = await db.note.findMany({ orderBy:{ createdAt:'desc' } });
  return (
    <main className="max-w-3xl mx-auto p-6 space-y-6">
      <h1 className="text-3xl font-bold">Notes</h1>
      <NewNoteForm/>
      <ul className="space-y-3">
        {notes.map(n=> <li key={n.id} className="rounded-xl border p-4 shadow-sm"><p className="font-semibold">{n.title}</p>{n.body && <p className="text-slate-600 text-sm mt-1">{n.body}</p>}</li>)}
      </ul>
    </main>
  );
}
```
`components/NewNoteForm.tsx` posts to `/api/notes` then refreshes.

## 5. Env before deploy
```bash
web_dev action=set_env project=./notes-app name=DATABASE_URL value="postgresql://..." environment=production
web_dev action=deploy project=./notes-app
```

## 6. Verify
Open the returned URL in a browser; create/edit/delete a note; confirm persistence across reloads.

This same skeleton scales to auth (add User + session cookie), relations (Post belongsTo User), and file uploads (Vercel Blob).