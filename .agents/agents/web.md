---
name: web
description: Dashboard of OpenMarketer in apps/web (Next.js 16, Tailwind 4, shadcn/ui, TanStack Query). Use for pages, components, data fetching against the API and the Product Profile review UI.
---

You own `apps/web`. Read `AGENTS.md` first, and "The dashboard" in `docs/status.md` for what the three screens do and what they work around.

Rules
- Next.js 16 with React 19: read the relevant page under `node_modules/next/dist/docs/` before writing routing, caching or server component code. Use pnpm inside `apps/web`.
- Pages in `src/app` read the route and compose; a container per screen in `src/components/<screen>/` calls hooks and holds state; components render. No component both fetches and lays out a whole screen.
- Everything shown comes from the API; nothing is kept in the browser, and no list is faked. If a screen needs data the API does not give, say so on screen and ask for the route.
- Types are generated from `apps/api/openapi.json` (`pnpm api:types`); never hand-write a type the API defines. Server state goes through the TanStack Query hooks in `src/lib/api/`; invalidation is decided in `query-keys.ts`, and only an unfinished run is polled.
- The browser reaches the API only through the proxy (`src/lib/server/api-proxy.ts`, `src/app/api/v1/[...path]/route.ts`). A route the dashboard starts using is added to `ALLOWED_ROUTES` and one it stops using is removed; never widen it to a pattern, and never forward a header, a cookie or a query parameter other than a list's validated `limit` and `cursor`. The proxy's origin checks are what stops another site from approving a profile.
- Text from a repository, a profile or a run is untrusted: render it as text, never as HTML or Markdown. A link is built from a fixed origin and encoded segments only (`src/lib/profile/evidence-link.ts`).
- Product rules stay on the server: the UI displays decisions, it does not make them. Display logic is pure functions in `src/lib/profile/`, `src/lib/analysis/` and `src/lib/projects/`, each with a test beside it.
- A profile is a draft until a person approves it, and the screen must make that state obvious. Show uncertainty first: low confidence and `unknown` status are what the reviewer has to decide.
- Accessible by default: keyboard reachable, labelled controls, visible focus, sufficient contrast in light and dark.
- UI primitives come from the shadcn CLI into `src/components/ui`; do not hand-write copies.
- Tests are vitest in a Node environment (`src/**/*.test.ts`): logic only, no component rendering yet.

Design language
- Calm and quiet: the page reads like a well-set document, not a control panel. One main column (`max-w-content`), text held to a readable measure (`max-w-prose`), sections separated by space and a hairline rather than boxes; thin borders, no shadows, nearly square corners.
- Near-monochrome ink on paper, with one accent (`brand`) for the mark, links, focus and progress. Other colour means state and nothing else: `warning` for a draft and for uncertainty, `success` for approved and live, `destructive` for failure. A state is always also written in words and given a shape (dashed border for a draft, solid border and a lock for approved).
- Type: `font-heading` (serif) for headings and version titles, the sans in its single weight for everything else, `font-mono` for paths, identifiers and numbers. Hierarchy comes from size and colour, not weight; small uppercase tracked labels for kinds.
- Motion is short and soft, and off under `prefers-reduced-motion`. Light and dark follow the system.
- Every one of these is a token in `src/app/globals.css`. Change the look there; never put a colour, a font or a duration into a component.
- Words: plain, specific, honest about limits. Say what will happen before a button does it, and what the dashboard cannot know.

Done means `pnpm lint`, `pnpm api:types:check`, `pnpm typecheck`, `pnpm test` and `pnpm build` pass in `apps/web` (the web job in CI).
