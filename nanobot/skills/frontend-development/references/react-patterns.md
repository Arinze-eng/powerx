# React patterns (TypeScript)

## Functional component with props
```tsx
type ButtonProps = { label: string; onClick?: () => void; variant?: 'primary' | 'ghost'; };
export function Button({ label, onClick, variant = 'primary' }: ButtonProps) {
  const base = 'rounded-lg font-semibold transition px-4 py-2';
  const styles = variant === 'primary' ? 'bg-indigo-600 text-white hover:bg-indigo-700' : 'border border-slate-300 hover:bg-slate-50';
  return <button className={`${base} ${styles}`} onClick={onClick}>{label}</button>;
}
```

## List rendering (stable keys)
```tsx
{items.map((item) => <li key={item.id}>{item.name}</li>)}
```

## Controlled form
```tsx
function Signup() {
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!/^\S+@\S+\.\S+$/.test(email)) return setError('Enter a valid email');
    setPending(true); setError(null);
    try { await fetch('/api/signup', { method:'POST', body: JSON.stringify({ email }) }); }
    catch { setError('Something went wrong'); } finally { setPending(false); }
  }
  return (
    <form onSubmit={submit} className="space-y-3">
      <input value={email} onChange={(e)=>setEmail(e.target.value)} placeholder="you@co.com"
        className="w-full rounded-lg border border-slate-300 px-3 py-2 focus:ring-2 focus:ring-indigo-200 outline-none"/>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button disabled={pending} className="rounded-lg bg-indigo-600 px-4 py-2 text-white disabled:opacity-50">
        {pending ? 'Submitting…' : 'Sign up'}
      </button>
    </form>
  );
}
```

## Custom hook for data + states
```tsx
function useFetch<T>(url: string) {
  const [data, setData] = useState<T|null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string|null>(null);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetch(url).then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP '+r.status)))
      .then(d => alive && setData(d))
      .catch(e => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [url]);
  return { data, loading, error };
}
```

## Modal / dialog
```tsx
function Modal({ open, onClose, children }: {open:boolean; onClose:()=>void; children:ReactNode}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl" onClick={e=>e.stopPropagation()} role="dialog" aria-modal>
        {children}
      </div>
    </div>
  );
}
```

## Conditional class helper
```ts
export const cx = (...c: (string|false|null|undefined)[]) => c.filter(Boolean).join(' ');
```