# Starter templates

## Vite + React + TS + Tailwind (SPA)
```bash
npm create vite@latest my-app -- --template react-ts
cd my-app
npm i -D tailwindcss postcss autoprefixer
npx tailwindcss init -p
```
`tailwind.config.js`:
```js
export default { content: ['./index.html','./src/**/*.{ts,tsx}'], theme:{extend:{}}, plugins:[] }
```
`src/index.css`:
```css
@tailwind base; @tailwind components; @tailwind utilities;
```
Run: `npm run dev`. Build for deploy: `npm run build` → `dist/`. Deploy `dist` via web_dev.

## Next.js 14 App Router (recommended for Vercel)
```bash
npx create-next-app@latest my-app --typescript --tailwind --app --eslint
```
Structure:
```
app/layout.tsx   # root layout + <html lang>, fonts, metadata
app/page.tsx     # home
app/api/route.ts # route handlers (backend)
components/ui/    # primitives
lib/              # helpers
```
Deploy folder = project root with Vercel auto-detecting Next.js.

## Single-file static site (no build)
```html
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>My Site</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
</head><body class="font-[Inter] text-slate-900 bg-white">
<!-- sections -->
</body></html>
```
Deploy the directory containing `index.html` directly.

## package.json scripts that work on Vercel
```json
{ "scripts": { "dev":"next dev", "build":"next build", "start":"next start" } }
```
Vercel runs `npm run build`; ensure it succeeds locally first (`web_dev action=deploy`).