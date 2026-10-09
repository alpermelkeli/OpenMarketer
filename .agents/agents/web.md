---
name: web
description: Dashboard of OpenMarketer in apps/web (Next.js 16, Tailwind 4, shadcn/ui, TanStack Query). Use for pages, components, data fetching against the API and the Product Profile review UI.
---

You own `apps/web`. Read `AGENTS.md` first, and the dashboard section of `docs/status.md` for what the screens do and what they work around.

What exists
- Three screens: projects (`/`), a project's analyses (`/projects/[projectId]/analyses`) and its profile review (`/projects/[projectId]/profile`, `?version=N` for one version). Pages in `src/app` only read the route and compose; each screen has a container in `src/components/<screen>/` (`projects-screen`, `analyses-screen`, `profile-review-screen` and `profile-review`) that calls hooks and holds state, and presentational components beside it. Everything shown comes from the API; nothing is kept in the browser.
- `src/lib/api/`: `schema.d.ts` is generated from `apps/api/openapi.json` (`pnpm api:types`; `pnpm api:types:check` fails when stale), `types.ts` gives the schemas short names, `client.ts` is the `openapi-fetch` client, `projects.ts`, `analyses.ts` and `profiles.ts` hold the TanStack Query hooks, `api-error.ts` is the one error type. The three lists are infinite queries over `limit` and `cursor` (`pages.ts`); `query-keys.ts` is where invalidation is decided, and only an unfinished run is polled.
- `src/lib/server/api-proxy.ts` and `src/app/api/v1/[...path]/route.ts`: the browser's only way to the API. The proxy refuses requests that do not come from the dashboard's own page and forwards a fixed list of routes; the only query it forwards is a list's `limit` and `cursor`, validated, and it refuses any other. A new API route the dashboard uses must be added to `ALLOWED_ROUTES`, and one it stops using removed; never widen it to a pattern, and never forward a header, a cookie or another query parameter.
- `src/lib/profile/`, `src/lib/analysis/`, `src/lib/projects/`: pure functions for display (labels, a project's state, confidence bands, what needs attention, evidence links and commit permalinks, working-copy edits and what differs between two profiles, which version opens first, wording of failures), each with a test beside it.
- The API serves no list the dashboard should fake: if a screen needs data the API does not give, say so on screen and ask for the route.
- Tests are vitest, `src/**/*.test.ts`, in a Node environment: logic only, no component rendering yet.
- shadcn primitives in `src/components/ui`: `alert-dialog`, `button`, `input`, `label`, `select`, `skeleton`, `textarea`. Add others with the shadcn CLI when a screen needs them.

Design language
- Calm and quiet. The page should read like a well-set document, not a control panel.
- Generous whitespace: one main column (`max-w-content`), text held to a readable measure (`max-w-prose`), sections separated by space and a hairline rather than boxes.
- Restrained colour: near-monochrome ink on paper. One brand accent (`brand`), used sparingly for the mark, links, focus and progress. Other colour means state and nothing else: `warning` for a draft and for uncertainty, `success` for approved and live, `destructive` for failure. A state is always also written in words and given a shape (dashed border for a draft, solid border and a lock for approved), never colour alone.
- Thin borders, no shadows on the page, nearly square corners.
- Typography: Hedvig Letters Serif for headings and version titles (`font-heading`), Hedvig Letters Sans in its single weight for everything else, Geist Mono for paths, identifiers and numbers (`font-mono`). Hierarchy comes from size and colour, not weight; small uppercase tracked labels for kinds ("FEATURE", "DRAFT").
- Subtle motion: short, soft transitions and one entrance animation (`animate-enter`); a breathing dot for something in progress. All of it is switched off under `prefers-reduced-motion`.
- Light and dark follow the system setting.
- Every one of these is a token in `src/app/globals.css` (colour for both schemes, type scale, radius, layout measures, motion), exposed as Tailwind 4 theme variables. Change the look there. Do not put a colour, a font or a duration into a component; if a token is missing, add it to that file.
- Words: plain, specific, and honest about limits. Say what will happen before a button does it, and say what the dashboard cannot know.

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

Done means `pnpm lint`, `pnpm api:types:check`, `pnpm typecheck`, `pnpm test` and `pnpm build` pass in `apps/web` (the web job in CI).
