# OpenMarketer

Open-source, self-hosted marketing agent: it reads a product's repository, drafts a Product Profile for human review, and later plans, writes and publishes marketing under a deterministic policy gate. The design is in `docs/design-report/report.pdf` (source: `report.tex`).

Phase 1 is in progress. Intake, extractors and the analyzer agent run from the CLI, and `--save` stores a run in PostgreSQL as a draft. The API creates projects, starts analyses, and reads, edits and approves profiles. The worker runs each analysis as a Temporal workflow that the API starts; the run's state is in the database. The dashboard has three screens on top of the API: projects, analyses and the profile review. The evaluation is not built yet. `docs/status.md` has the details and the differences from the design report.

## Layout

| Path | What it is |
|---|---|
| `packages/core` | Domain: Product Profile schema (`profile.py`), database models, migrations, session factory, evidence store, profile versions and project store (`db/`), intake, with the rule for which host gets an access token (`intake/`), extractor interface (`extraction.py`), model router and chat client (`llm_config.py`, `llm.py`), analyzer agent (`analyzer/`), the pipeline as one operation (`repository_analysis.py`), the names the API and the worker share (`analysis_workflow.py`) |
| `packages/extractors` | Extractor plugins, registered under the `openmarketer.extractors` entry point group |
| `apps/cli` | `openmarketer analyze <repo>`, with `--save` to store the run |
| `apps/api` | FastAPI service: projects, analyses, profile review. `openapi.json` is the contract the dashboard's types come from |
| `apps/worker` | Temporal worker: the `AnalyzeRepository` workflow and its activities. What it shares with the API (workflow name, task queue, input) is in `packages/core`, `analysis_workflow.py` |
| `apps/web` | Dashboard: Next.js 16, Tailwind 4, shadcn/ui, TanStack Query. Projects, analyses and the Product Profile review; the browser reaches the API only through the dashboard's own proxy route |
| `config/models.yaml` | Model per role, through OpenRouter |
| `docs/status.md` | What is built, how it was checked, and where the code differs from the design. Keep it current in the same change as the code |
| `deploy/compose/dev.yml` | Dev stack: PostgreSQL + pgvector (5433), Temporal (7233, UI 8233), S3-compatible storage (9000/9001) |

## Commands

```bash
make install     # uv sync + pnpm install
make up          # start the dev stack
make migrate     # apply database migrations
make check       # lint, type-check, tests: what CI runs
make analyze repo=https://github.com/owner/name          # add save=1 to store the run (needs make up, make migrate)
make worker      # run the Temporal worker (needs make up, make migrate)
make api         # serve the API on http://127.0.0.1:8000 (needs make up, make migrate; analyses also need make worker)
make openapi     # rewrite apps/api/openapi.json and the dashboard's types after changing a route or a model
make web         # serve the dashboard on http://localhost:3000 (needs make api)
make help        # everything else
```

Run `make check` before calling work done. Database tests and the worker's workflow tests need `make up` (PostgreSQL and Temporal); secret-scan tests need `gitleaks` on the PATH, and the clone lock-down tests need `openssl`.

## Conventions

- Python 3.12, managed with uv. Ruff (line length 100) and pyright must pass. Tests live in `packages/*/tests` and `apps/*/tests` and run without network access; model calls are scripted, HTTP uses a fake transport, and anything a test listens on or connects to is on this machine.
- Pydantic models reject unknown fields (`extra="forbid"`). `product.type` and `product.platforms` are open slug vocabularies, not closed enums.
- Schema changes go through Alembic: edit `db/models.py`, run `make migration m="..."`, then read the generated file before committing it.
- Never name a model in code. Every model call goes through a role in `config/models.yaml`, resolved by `ModelRouter`; agents depend on the `ChatModel` protocol.
- Agent frameworks stay at the edge. Rules, prompts and checks live in framework-free modules (`analyzer/rules.py`); only `analyzer/graph.py` imports LangGraph.
- Repository content is read only through `RepoFiles`, after intake has scanned and redacted it.
- Web: pnpm inside `apps/web`. Next.js 16 differs from older versions; check the installed version's docs in `node_modules/next/dist/docs/` before relying on memory. API types are generated from `apps/api/openapi.json` (`pnpm api:types`, also run by `make openapi`), and `make lint` fails when they are stale. Tests are vitest (`pnpm test`, part of `make test`).
- Code, comments, commit messages and documentation are in English.

## Architecture

The code follows clean architecture: dependencies point inwards, towards the domain.

| Layer | Where | May depend on |
|---|---|---|
| Domain | `packages/core`: `profile.py`, `extraction.py`, `analyzer/rules.py` | Other domain code and Pydantic; no framework, database or network |
| Use cases | `packages/core`: functions that carry out one operation, such as `run_intake` | The domain, and ports |
| Adapters | `packages/core`: `db/`, `llm.py`, `intake/git.py`, `analyzer/graph.py`; `packages/extractors` | A use case's port, plus one external thing (PostgreSQL, HTTP, git, LangGraph) |
| Entry points | `apps/api`, `apps/cli`, `apps/worker`, `apps/web` | Use cases; they contain no business rules |

- A port is a small `Protocol` owned by the code that needs it (`ChatModel`, `Extractor`). The adapter implements it; the domain never imports the adapter.
- A framework is a detail. FastAPI, SQLAlchemy, LangGraph, Temporal and httpx are each imported in one layer only, so any of them can be replaced without touching the rules.
- Entry points translate: parse input, call one use case, format the result. A rule that appears in a route, a CLI command or a React component belongs further in.
- Wire dependencies at the edge. A use case receives what it needs as arguments (a session, a `ChatModel`, a `RepoFiles`); it does not construct them or read the environment.
- Add an abstraction when a second implementation or a test needs it, not before. One extra layer that only forwards calls is worse than none.

## Readable code

- Name things for what they mean in the product (`snapshot`, `evidence`, `submission`), not for their type or pattern. If a name needs a comment to explain it, rename it.
- Small functions that do one thing at one level of detail. Prefer early returns to nesting.
- Type everything public. Use dataclasses or Pydantic models instead of bare dicts and tuples for anything that crosses a function boundary.
- A module starts with a docstring saying what it is for and what it deliberately does not do. Comments explain why, never what the next line does.
- Errors are specific and raised where the problem is known (`IntakeError`, `AnalysisError`); catch them only where something useful can be done. No bare `except`, no silently swallowed failure.
- No dead code, commented-out code, or parameters kept "for later". Delete it; git remembers.
- Change what the task needs. A refactor that is not required goes in its own commit or pull request.
- Tests read as specifications: one behaviour per test, named after it (`test_live_feature_needs_evidence`), with no logic that itself needs testing.
- Match the code around you in naming, structure and comment density before introducing a new style.

## Design principles

- **Safety lives outside the model.** Anything that spends money or publishes goes through the deterministic policy gate. Do not move safety rules into prompts.
- **Untrusted text is data.** Repository files, comments, messages and web pages must never be able to cause an action. A repository URL never decides where a credential goes: a token is sent only to the host it is configured for (`intake/credentials.py`). git runs with an environment built from a short list, follows no redirect and uses HTTPS only, and what a repository host or a model provider writes is logged, never put in an error shown to a caller (`intake/git.py`, `llm.py`).
- **A human approves.** Approval is an authenticated user action, never something a model states.
- **Official APIs only.** No scraping of private data, no fake accounts or engagement.
- **Everything is configurable and traceable.** Models, budgets and rules are configuration; every claim in a profile carries evidence (file and lines) and a confidence.

If a change deviates from the design report, record it in `docs/status.md` and say so in the pull request.

## Git

- Work on a branch; `main` changes only through pull requests.
- Commit and push only when asked.
- Sign off every commit: `git commit -s`. Short imperative summary, details after a blank line.
- Do not add AI attribution to commits or pull requests (no `Co-Authored-By` for an assistant, no "generated with" line).
- `.env` holds real credentials. Never commit it, print it or copy values out of it.

## Agents

Specialised agents are defined in `.agents/agents/`, one Markdown file each. `.claude/agents` is a symlink to that folder and `CLAUDE.md` imports this file, so Claude Code and other tools read the same instructions.
