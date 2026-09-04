# Security hardening

## CORS (Express)
```ts
import cors from 'cors';
app.use(cors({ origin: process.env.ALLOWED_ORIGINS?.split(',') ?? false, credentials: true }));
```
Set `ALLOWED_ORIGINS=https://your-app.vercel.app` via `web_dev set_env`.

## Security headers (Next.js middleware)
```ts
// middleware.ts
import { NextResponse } from 'next/server';
export function middleware(req: Request) {
  const res = NextResponse.next();
  res.headers.set('X-Content-Type-Options','nosniff');
  res.headers.set('Referrer-Policy','strict-origin-when-cross-origin');
  res.headers.set('Permissions-Policy','geolocation=(), microphone=()');
  return res;
}
```

## Rate limiting (in-memory demo; use Upstash/Redis in prod)
```ts
const hits = new Map<string,{c:number;t:number}>();
function rateLimit(ip:string, max=60, windowMs=60_000){
  const now=Date.now(); const e=hits.get(ip);
  if(!e || now-e.t>windowMs){ hits.set(ip,{c:1,t:now}); return true; }
  e.c++; return e.c<=max;
}
```

## Webhook signature verification (Stripe example)
```ts
import Stripe from 'stripe';
const sig = req.headers['stripe-signature'];
try { event = stripe.webhooks.constructEvent(rawBody, sig!, process.env.STRIPE_WEBHOOK_SECRET!); }
catch { return new Response('invalid signature', { status: 400 }); }
```

## Checklist
- [ ] Secrets only in env (`process.env.X`), never committed or sent to browser.
- [ ] DB queries parameterized / ORM — no raw concatenation.
- [ ] Auth endpoints rate-limited.
- [ ] HTTPS enforced (Vercel does this automatically).
- [ ] Error responses generic in production (no stack traces).
- [ ] File uploads validated (type, size) and stored safely.
- [ ] SQL injection / XSS / CSRF mitigated (use framework protections).