# Data fetching with full state handling

## Client (React) — handle loading / error / empty / retry
```tsx
export function UserList() {
  const [state, setState] = useState<{status:'idle'|'loading'|'ok'|'error'; data?:User[]; error?:string}>({status:'idle'});
  async function load() {
    setState({status:'loading'});
    try {
      const r = await fetch('/api/users');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data: User[] = await r.json();
      setState({status:'ok', data});
    } catch (e) { setState({status:'error', error:(e as Error).message}); }
  }
  useEffect(() => { load(); }, []);
  if (state.status==='loading') return <Skeleton rows={5}/>;
  if (state.status==='error') return <div className="text-red-600">{state.error} <button onClick={load}>Retry</button></div>;
  if (!state.data?.length) return <p>No users yet.</p>;
  return <ul>{state.data.map(u=> <li key={u.id}>{u.name}</li>)}</ul>;
}
```

## Next.js Server Component (no client JS needed)
```tsx
// app/page.tsx
async function getPosts() {
  const res = await fetch('https://api.example.com/posts', { next: { revalidate: 60 } }); // cache + ISR
  if (!res.ok) throw new Error('Failed to load posts');
  return res.json();
}
export default async function Page() {
  const posts = await getPosts();
  return <ul>{posts.map((p:{id:string;title:string}) => <li key={p.id}>{p.title}</li>)}</ul>;
}
```

## Route handler that calls an external API (Next App Router)
```ts
// app/api/search/route.ts
export async function GET(req: Request) {
  const q = new URL(req.url).searchParams.get('q') ?? '';
  const upstream = process.env.SEARCH_API_KEY; // set via web_dev set_env
  if (!upstream) return Response.json({ error: 'missing SEARCH_API_KEY' }, { status: 500 });
  const r = await fetch(`https://api.search.dev/?q=${encodeURIComponent(q)}`, { headers:{ Authorization:`Bearer ${upstream}` }});
  return Response.json(await r.json(), { status: r.status });
}
```

## Rules
- Always cancel/ignore stale requests on unmount (the `alive` flag above).
- Use `next.revalidate` or SWR/React Query for caching server data.
- Put secrets in route handlers (server), never expose them to the browser.
- Public env vars must be prefixed `NEXT_PUBLIC_`; everything else stays server-side.