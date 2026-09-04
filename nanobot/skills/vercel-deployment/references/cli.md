# Vercel CLI command reference (token auth)

All commands run with `--token $VERCEL_TOKEN` (the web_dev tool injects it). Run them via
the sandbox/exec if you need a raw CLI; otherwise use `web_dev`.

## Auth & scope
```bash
vercel whoami --token $VERCEL_TOKEN
vercel ls --token $VERCEL_TOKEN                 # list deployments in current scope
vercel ls <project> --token $VERCEL_TOKEN       # deployments for a project
vercel inspect <url-or-id> --token $VERCEL_TOKEN  # details of a deployment/project
```

## Deploy
```bash
vercel deploy --yes --token $VERCEL_TOKEN                 # preview+prod, skip prompts
vercel deploy --prod --yes --token $VERCEL_TOKEN          # force production
vercel deploy --build-env KEY=val --env KEY=val --token $VERCEL_TOKEN   # inline vars
```
Flags: `-y/--yes`, `--prod`, `-e/--env KEY=VALUE` (runtime), `--build-env KEY=VALUE`,
`-S/--scope <team>`, `-t/--token <TOKEN>`.

## Environment variables
```bash
echo "value" | vercel env add NAME production --token $VERCEL_TOKEN
vercel env ls --token $VERCEL_TOKEN
vercel env pull .env.local --token $VERCEL_TOKEN     # download to local file
vercel env rm NAME production --token $VERCEL_TOKEN
```
Environments: `production`, `preview`, `development`. Values are hidden when sensitive.

## Projects / domains
```bash
vercel project ls --token $VERCEL_TOKEN
vercel project add <name> --token $VERCEL_TOKEN
vercel domains add example.com --token $VERCEL_TOKEN
vercel domains ls --token $VERCEL_TOKEN
vercel alias set <deployment-url> my-domain.vercel.app --token $VERCEL_TOKEN
```

## Remove
```bash
vercel remove <project> --yes --token $VERCEL_TOKEN   # delete project + deployments
vercel rm <url> --yes --token $VERCEL_TOKEN           # delete a single deployment
```

## Logs & status
```bash
vercel logs <deployment-url> --token $VERCEL_TOKEN    # stream build/runtime logs
vercel inspect <url> --wait --token $VERCEL_TOKEN     # wait until ready
```

## Notes
- The first deploy from a folder creates/links the project automatically (`--yes`).
- Telemetry notices print to stderr; ignore them — parse stdout for the URL.
- Always pass `--yes` to avoid interactive prompts hanging an automated flow.