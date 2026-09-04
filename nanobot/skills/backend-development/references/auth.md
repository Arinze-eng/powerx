# Authentication patterns

## Password hashing (bcrypt)
```ts
import bcrypt from 'bcryptjs';
const hash = await bcrypt.hash(password, 10);
const ok = await bcrypt.compare(password, user.passwordHash);
```

## Issue a JWT + httpOnly cookie (Next route handler)
```ts
// app/api/auth/login/route.ts
import { NextResponse } from 'next/server';
import jwt from 'jsonwebtoken';
import bcrypt from 'bcryptjs';
import { db } from '@/lib/db';

export async function POST(req: Request) {
  const { email, password } = await req.json();
  const user = await db.user.findUnique({ where: { email } });
  if (!user || !(await bcrypt.compare(password, user.passwordHash)))
    return NextResponse.json({ error: 'Invalid credentials' }, { status: 401 });

  const token = jwt.sign({ sub: user.id }, process.env.JWT_SECRET!, { expiresIn: '7d' });
  const res = NextResponse.json({ id: user.id, email: user.email });
  res.cookies.set('session', token, {
    httpOnly: true, sameSite: 'lax', secure: process.env.NODE_ENV === 'production', path: '/', maxAge: 60*60*24*7,
  });
  return res;
}
```

## Verify on each request (middleware / helper)
```ts
import jwt from 'jsonwebtoken';
export function requireUser(req: Request) {
  const token = req.headers.cookie?.match(/session=([^;]+)/)?.[1];
  if (!token) throw new Error('unauthorized');
  try { return jwt.verify(token, process.env.JWT_SECRET!); }
  catch { throw new Error('unauthorized'); }
}
```

## Logout (clear cookie)
```ts
const res = NextResponse.json({ ok: true });
res.cookies.delete('session');
return res;
```

## Rules
- `JWT_SECRET` comes from env (`web_dev set_env JWT_SECRET ...`), never hardcoded.
- Cookies are httpOnly so JS can't read them (mitigates XSS token theft).
- Use short expiry for access tokens; rotate refresh tokens server-side.
- Return generic auth errors ("Invalid credentials"), not "user not found".