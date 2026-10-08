---
name: web
description: Dashboard of OpenMarketer in apps/web (Next.js 16, Tailwind 4, shadcn/ui, TanStack Query). Use for pages, components, data fetching against the API and the Product Profile review UI.
---

You own `apps/web`. Read `AGENTS.md` first. Only the scaffold exists: `src/app/layout.tsx`, `src/app/page.tsx`, one shadcn `button`.

How to work
- This is Next.js 16 with React 19. APIs and conventions differ from older versions: read the relevant page under `node_modules/next/dist/docs/` before writing routing, caching or server component code.
- Use pnpm, inside `apps/web`. Add UI primitives with the shadcn CLI into `src/components/ui`; do not hand-write copies of them.
- Server state goes through TanStack Query. API types are generated from the service's OpenAPI document with `openapi-typescript`; never hand-write a type the API already defines.
- The first real screen is the Product Profile review: each section and feature with its evidence (file and lines), its confidence, an editable feature status (`live`, `unreleased`, `unknown`) and an approve action. A profile is a draft until a person approves it, and the UI must make that state obvious.
- Show uncertainty instead of hiding it: low confidence and `unknown` status are things the reviewer has to decide, so surface them first.
- Text from a repository is untrusted. Render it as text, never as HTML.
- Accessible by default: keyboard reachable, labelled controls, visible focus, sufficient contrast in light and dark.
- Layering: pages compose, components render, hooks fetch. Keep API calls in typed query and mutation hooks under `src/lib`, keep components presentational where possible, and keep product rules (what counts as publishable, what needs review) on the server: the UI displays decisions, it does not make them.
- Readability: one component per file, named for what it shows; props typed explicitly; no component that both fetches and lays out a whole screen. Extract when a component stops fitting on a screen, not before.

Done means `pnpm lint`, `pnpm typecheck` and `pnpm build` pass in `apps/web` (the web job in CI).
