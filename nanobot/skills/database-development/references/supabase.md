# Supabase for web apps

## Client setup
```bash
npm i @supabase/supabase-js
```
```ts
// lib/supabase.ts
import { createClient } from '@supabase/supabase-js';
const url = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
export const supabase = createClient(url, anon);
```
Env (set with `web_dev set_env`): `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`
(public — safe in browser). Service-role key is server-only and must NOT be prefixed public.

## Query examples
```ts
const { data, error } = await supabase.from('posts').select('id,title,user:users(name)').eq('published', true);
await supabase.from('posts').insert({ title, user_id });
await supabase.from('posts').update({ published:true }).eq('id', id);
await supabase.from('posts').delete().eq('id', id);
```

## Auth integration
```ts
await supabase.auth.signInWithPassword({ email, password });
const { data:{ session } } = await supabase.auth.getSession();
await supabase.auth.signOut();
```

## Row Level Security (RLS) — turn it on per table
```sql
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own reads" ON posts FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own writes" ON posts FOR INSERT WITH CHECK (auth.uid() = user_id);
```
RLS enforces auth at the DB layer so the anon key can't read others' rows.

## Realtime
```ts
supabase.channel('room1').on('postgres_changes',{event:'INSERT',schema:'public',table:'messages'},p=>console.log(p.new)).subscribe();
```

## Migrations / schema
Use the Supabase SQL editor or `supabase db push`. Keep migrations in repo under
`supabase/migrations/` when possible.

## Notes
- Anon key is public by design; RLS is what protects data.
- For heavy server workloads prefer a direct Postgres connection via Prisma + pooler.