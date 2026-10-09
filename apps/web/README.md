# OpenMarketer dashboard

The web interface of OpenMarketer: see every project and where it stands, start an analysis of a repository and follow it, then review the drafted Product Profile, with the evidence and confidence behind every claim, correct it, approve it, and look back through its versions. Next.js 16, React 19, Tailwind 4, shadcn/ui (Base UI), TanStack Query.

What it does, what it works around and how it was checked is in [docs/status.md](../../docs/status.md#the-dashboard).

## Running it

```bash
make api      # from the repository root: the API on http://127.0.0.1:8000
make worker   # only needed to run an analysis
make web      # the dashboard on http://localhost:3000
```

Inside `apps/web`:

```bash
pnpm dev               # development server, on 127.0.0.1 only
pnpm lint              # eslint
pnpm typecheck         # route types and tsc
pnpm test              # vitest
pnpm build             # production build
pnpm api:types         # regenerate src/lib/api/schema.d.ts from ../api/openapi.json
pnpm api:types:check   # fail if that file is stale
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPENMARKETER_API_URL` | `http://127.0.0.1:8000` | Where the dashboard's server finds the API. Read on the server only; the browser never sees it |

## Do not expose it

There is no login. The API trusts requests from this machine, and the dashboard's proxy (`src/lib/server/api-proxy.ts`) trusts requests from the dashboard's own page. Both listen on `127.0.0.1`. Binding the dashboard to another address, or putting it behind a reverse proxy, lets anyone who can reach it approve a profile.

## Where things are

| Path | What it is |
|---|---|
| `src/app` | Routes. Pages read the route and compose a screen; `api/v1/[...path]/route.ts` is the proxy to the API |
| `src/app/globals.css` | Every design token: colour (light and dark), type, radius, layout measures, motion |
| `src/components/<screen>` | One container per screen, and the presentational components it composes |
| `src/components/ui` | shadcn primitives, added with the shadcn CLI |
| `src/lib/api` | Generated types, the typed client, query and mutation hooks |
| `src/lib/server` | The proxy's rules |
| `src/lib/profile`, `src/lib/analysis`, `src/lib/projects` | Pure display logic, with tests |

The typefaces are Hedvig Letters Sans and Hedvig Letters Serif (SIL Open Font License 1.1) and Geist Mono (SIL Open Font License 1.1), loaded through `next/font`.
