# Validation with Zod

## Define schemas per resource
```ts
import { z } from 'zod';
export const CreateUser = z.object({
  email: z.string().email(),
  name: z.string().min(1).max(80),
  role: z.enum(['user','admin']).default('user'),
});
export type CreateUserInput = z.infer<typeof CreateUser>;
```

## Validate + sanitize in a handler
```ts
const body = await req.json().catch(() => null);
const parsed = CreateUser.safeParse(body);
if (!parsed.success) {
  return NextResponse.json({ error: 'validation_failed', issues: parsed.error.flatten() }, { status: 400 });
}
// use parsed.data (typed, defaulted, stripped of unknown keys)
```

## Error shape convention
```json
{ "error": "validation_failed", "issues": { "fieldErrors": { "email": ["Invalid email"] }, "formErrors": [] } }
```

## Query param validation
```ts
const ListQuery = z.object({ limit: z.coerce.number().int().min(1).max(100).default(20), offset: z.coerce.number().int().min(0).default(0) });
const q = ListQuery.parse(Object.fromEntries(new URL(req.url).searchParams));
```

## Rules
- Validate at the boundary; trust validated data internally.
- Use `.strict()` to reject unexpected fields on sensitive writes.
- Coerce types for query params (`z.coerce.number()`).
- Never echo raw user input into HTML responses without escaping.