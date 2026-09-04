# Environment variables on Vercel

## Three scopes
- **production** — used by the live production deployment.
- **preview** — used by PR/branch deployments.
- **development** — used by `vercel dev` / local pulls.

Set with `web_dev action=set_env project=<dir> name=<VAR> value=<val> environment=<scope>`.

## Public vs secret
Anything the browser needs MUST be prefixed so it's inlined at build time:
- Next.js: `NEXT_PUBLIC_*`
- Vite/CRA/SvelteKit: `VITE_*` (Vite) or `REACT_APP_*` (CRA)
Everything else is server-only and safe as a secret (`DATABASE_URL`, `JWT_SECRET`, API keys).

Never expose a real secret to the client even with a prefix — that leaks it. Use route
handlers/server functions to proxy privileged calls instead.

## Required vars per common app
```bash
# Postgres + auth full-stack
web_dev action=set_env project=./app name=DATABASE_URL value="postgresql://..." environment=production
web_dev action=set_env project=./app name=DIRECT_URL value="postgresql://..." environment=production   # Prisma pooler
web_dev action=set_env project=./app name=JWT_SECRET value="<random>" environment=production
web_dev action=set_env project=./app name=NEXT_PUBLIC_SITE_URL value="https://app.vercel.app" environment=production

# Supabase
web_dev action=set_env project=./app name=NEXT_PUBLIC_SUPABASE_URL value="https://xxx.supabase.co" environment=production
web_dev action=set_env project=./app name=NEXT_PUBLIC_SUPABASE_ANON_KEY value="eyJ..." environment=production
web_dev action=set_env project=./app name=SUPABASE_SERVICE_ROLE_KEY value="eyJ..." environment=production  # server-only, NOT public

# Stripe / OpenAI / etc.
web_dev action=set_env project=./app name=STRIPE_SECRET_KEY value="sk_live_..." environment=production
web_dev action=set_env project=./app name=OPENAI_API_KEY value="sk-..." environment=production
```

## Order of operations
1. Set env vars.
2. Deploy (build reads them; runtime uses them).
3. If you change env later → redeploy for it to take effect.

## Verify
`web_dev action=status project=./app` lists env vars (sensitive values masked).

## Generate strong secrets
```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```