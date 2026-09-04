---
name: frontend-development
description: Build production-grade web frontends — HTML/CSS/JS fundamentals, React & Next.js, Tailwind CSS, state management, data fetching from APIs, accessibility, performance, and responsive UIs. Use when writing or reviewing any client-side code, single-page apps, component libraries, or wiring a UI to a backend/API. Pairs with web-design (look) and vercel-deployment (ship).
metadata:
  nanobot:
    always: false
---

# Frontend Development

Build fast, accessible, maintainable user interfaces. Choose the simplest stack that
meets the requirement, then apply professional structure and polish.

## Stack selection (decide first)

| Need | Pick |
| --- | --- |
| Marketing page / one-off landing | Single `index.html` + Tailwind CDN (see `web-design`) |
| Interactive app, no SSR needed | Vite + React + TypeScript + Tailwind |
| Content site needing SEO/routing | Next.js (App Router) + Tailwind |
| Prototype in one file | Plain HTML + Alpine.js + Tailwind |

Default to **Next.js + Tailwind** for anything the user wants deployed to Vercel, since
Vercel has first-class Next.js support. See `vercel-deployment`.

## Core rules

- **Mobile-first** CSS; add breakpoints upward (`sm md lg xl`).
- **Componentize**: small, focused components; one concern each.
- **Type everything** in TS; avoid `any`.
- **No inline styles** except dynamic values; use utility classes or tokens.
- **State lives as low as possible**; lift only when shared.
- **Fetch on the server** (Next.js RSC / route handlers) when you can; client-fetch only for interactivity.
- **Accessibility is non-negotiable**: semantic tags, ARIA only when needed, keyboard operable.

## Project layout (React/Next)

```
src/
  app/            # Next App Router pages & layouts
    page.tsx
    api/          # route handlers (backend)
  components/     # reusable UI
    ui/           # primitives (Button, Card, Input)
    features/     # domain widgets
  lib/            # helpers, API clients, utils
  hooks/          # custom hooks
  styles/         # globals.css (Tailwind directives + tokens)
```

For full templates see `references/templates.md`.

## Data fetching patterns

- Client fetch with loading/error/empty states — see `references/fetching.md`.
- Server components (Next): `const data = await fetch(...)` directly in the component.
- Always handle: loading spinner/skeleton, error message, empty result, retry.
- Never swallow errors; surface them to the user.

## State management

- Local `useState`/`useReducer` first.
- Context for cross-cutting concerns (theme, auth, cart).
- External store (Zustand) only when prop-drilling hurts.
- Server cache (React Query/SWR) for remote data; don't duplicate into global state.

## Forms

- Controlled inputs with validation; show field-level errors.
- Disable submit while pending; optimistic feedback after success.
- Use `<label>` bound to input `id`; required fields marked.

## Performance

- Code-split routes; lazy-load heavy components (`next/dynamic`, `React.lazy`).
- Optimize images (`next/image`, width/height, formats).
- Minimize client JS; prefer server rendering where possible.
- Debounce/throttle expensive handlers.

## Common mistakes to fix

- Missing keys in lists → unstable re-renders.
- Fetching in `useEffect` without cleanup/cancellation → race conditions.
- Large components doing everything → split.
- Unstyled default buttons/inputs → design tokens.
- No loading/error UI → looks broken during fetches.
- Hardcoded API URLs → use env vars (`NEXT_PUBLIC_*`) set via `web_dev set_env`.

## Reference files

- `references/react-patterns.md` — component/hook patterns, forms, lists, modals.
- `references/fetching.md` — client/server data fetching with full state handling.
- `references/templates.md` — copy-paste starter structures (Vite React, Next App Router).