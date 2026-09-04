# vercel.json recipes by framework

Place `vercel.json` in the project root. Most frameworks need nothing; use these only for overrides.

## Static HTML/CSS/JS (no build)
```json
{}
```
Just deploy the folder containing `index.html`. Optional cache headers:
```json
{ "headers": [{ "source": "/(.*)", "headers": [{ "key":"Cache-Control","value":"public, max-age=3600" }] }] }
```

## Next.js (App Router)
```json
{ "framework": "nextjs" }
```
Usually auto-detected. Set Node version in `package.json`:
```json
{ "engines": { "node": "20.x" } }
```

## Vite / React SPA
```json
{ "buildCommand": "npm run build", "outputDirectory": "dist",
  "rewrites": [{ "source": "/(.*)", "destination": "/index.html" }] }
```
The rewrite makes client-side routing work on refresh.

## Create React App
```json
{ "buildCommand": "CI=false npm run build", "outputDirectory": "build",
  "rewrites": [{ "source": "/(.*)", "destination": "/index.html" }] }
```

## Astro (static or SSR)
```json
{ "framework": "astro", "buildCommand": "npm run build", "outputDirectory": "dist" }
```

## SvelteKit (Node adapter → serverless)
```json
{ "buildCommand": "npm run build", "outputDirectory": "build" }
```
Use `@sveltejs/adapter-vercel` in `svelte.config.js`.

## Express API (serverless function)
```json
{ "version": 2, "builds": [{ "src": "server.js", "use": "@vercel/node" }],
  "routes": [{ "src": "/(.*)", "dest": "server.js" }] }
```
`server.js` must `export default app` (the Express instance).

## Python API (FastAPI)
```json
{ "builds": [{ "src": "main.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "main.py" }] }
```
`main.py` exposes an `app` object.

## Redirects & rewrites
```json
{ "redirects": [{ "source":"/old", "destination":"/new", "permanent":true }],
  "rewrites": [{ "source":"/api/(.*)", "destination":"/backend/api/$1" }] }
```

## Headers (CORS/security)
```json
{ "headers": [{ "source":"/(.*)", "headers":[
  {"key":"X-Frame-Options","value":"DENY"},
  {"key":"Access-Control-Allow-Origin","value":"https://your.app"} ]}] }
```