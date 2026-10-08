# OpenMarketer

**Give it your repository. It markets your app.**

OpenMarketer is an open-source AI agent that reads the source repository of any mobile app or website, builds a verified understanding of the product, and runs the marketing: research, content, publishing, ad campaigns and measurement. It asks you before anything risky or expensive, and it cannot spend beyond the budget you set.

![License](https://img.shields.io/github/license/alpermelkeli/OpenMarketer)

> **Status: early development.** The architecture, safety model and roadmap are designed and documented. The first milestone, the repository analyzer, runs from the command line or through a local API and produces a draft Product Profile, which can be stored in a local database; nothing is reviewed or published yet. See [what exists today](#status).

<p align="center">
  <img src="docs/design-report/figures/fig_loop.png" alt="The OpenMarketer agent loop" width="640">
</p>

## Why another marketing tool?

Most tools automate one slice: scheduling, ad rules or copy generation. Strategy and coordination stay with you. OpenMarketer is built around a different set of ideas:

- **Generic by construction.** Point it at any repository. It extracts a structured, versioned, human-approved *Product Profile* (features, tone, languages, business model, measurement setup) with file-and-line evidence for every claim. Strategy comes from replaceable playbooks, not from a hand-written brief.
- **Safety outside the model.** Budget caps, approval rules and content filters are deterministic code at the tool boundary, not instructions in a prompt. A transactional ledger makes it impossible to exceed a cap, even with concurrent actions.
- **You stay in control.** Four autonomy levels, from "suggest only" to "capped autonomy", promoted only by you. A kill switch stops everything. Only an authenticated human click approves an action; a model saying "approved" never does.
- **Hostile text cannot steer it.** Comments, DMs, web pages and README files go through a quarantined reader agent that has no tools and returns only schema-validated fields.
- **A model per role, through OpenRouter.** Every LLM call site (writer, planner, reader, orchestrator, and so on) has its own configurable model, with fallbacks and capability checks.
- **Talk to it.** A dashboard chat (the orchestrator) answers questions about the project and takes special requests. In proactive mode it also sends digests, approval reminders, questions and alerts.
- **An agentic creative studio.** Asset production works like a coding agent for graphics: it writes code to compose exact text, logos and layouts, generates backgrounds or whole images with models, then verifies the result (OCR, contrast, size checks and a separate vision model) and edits until it passes or hands over to a human, within iteration and cost limits.
- **Agent graphs with durable workflows.** Agent behaviour is written as LangGraph graphs that run inside Temporal activities: Temporal owns time and reliability, LangGraph owns the reasoning flow of a task.
- **Pluggable everything.** Platforms, media providers (fal.ai, Higgsfield, ElevenLabs), framework extractors, playbooks and policy packs are plugins behind small interfaces.

## Status

| Area | Status |
|---|---|
| Design report: architecture, safety model, data model, security, roadmap | Done ([PDF](docs/design-report/report.pdf), 54 pages) |
| Per-role LLM model configuration through OpenRouter | Built and used by the analyzer (`config/models.yaml`) |
| Repository analyzer and Product Profile | **In progress**: intake, extractors and the analyzer agent run from the CLI, which can store a run as a draft; the API registers projects, runs analyses, and reads, edits and approves profiles; the review UI and evaluation are not built |
| Content engine and approved publishing (X, Instagram) | Planned |
| Creative studio (compose with code, generate, verify, edit) | Designed |
| Orchestrator console and proactive mode | Designed |
| Ad connectors (recommend-only first) | Planned |
| Plugin SDK and playbook packs | Planned |

Roadmap in one line: Product Profile from any repo, then approved publishing, then community and analytics, then ads, then ecosystem. Details are in the report. What is built, how it was checked and where it differs from the design is in [docs/status.md](docs/status.md).

## How it works

<p align="center">
  <img src="docs/design-report/figures/fig_arch.png" alt="System architecture" width="520">
</p>

1. **Understand.** Read-only clone, secret scan, an analyzer agent that explores the repository (with deterministic extractors as hints), human review.
2. **Plan and create.** Weekly plan from a playbook; text, images, short video and voice-over through replaceable providers.
3. **Control.** Every action passes the policy engine: banned content, budget caps, content filter, then a risk level that decides between automatic execution and human review.
4. **Act and learn.** Publish through connectors, measure, write results to memory, repeat.

## Try what exists today

You need [uv](https://docs.astral.sh/uv/), [gitleaks](https://github.com/gitleaks/gitleaks) and an [OpenRouter](https://openrouter.ai) API key.

```bash
cp .env.example .env                 # add your OPENROUTER_API_KEY
uv sync
make analyze repo=https://github.com/owner/name
```

This clones the repository, removes secrets, lets the analyzer agent read the code and prints a draft Product Profile as JSON: product, features with their status, brand, audience, business model and measurement, each with file-and-line evidence and a confidence. It is a draft for a person to review, and it is not stored unless you ask for that (below). A private repository needs `GITHUB_TOKEN` (github.com) or `GITLAB_TOKEN` (gitlab.com, or the host in `GITLAB_HOST`) in `.env`. A token is sent only to its own host; cloning a real private repository is untested. Redirects are not followed, so use the address a repository has now, and on gitlab.com one that ends in `.git`.

To keep the result, store the run in the development database. This also needs Docker, and `DATABASE_URL` in `.env` set to `postgresql+psycopg://openmarketer:openmarketer@localhost:5433/openmarketer`:

```bash
make up                              # start PostgreSQL and the rest of the dev stack
make migrate                         # create the tables
make analyze repo=https://github.com/owner/name save=1
make psql                            # look at the tables: project, repo_snapshot, evidence, product_profile
```

Each stored run adds a snapshot of the commit, the extractor facts and a new draft version of the profile. A stored profile can be edited and approved through the API; there is no screen for it yet. Example queries and the limits of what is stored are in [docs/status.md](docs/status.md#storing-a-run).

`make api` serves the API on `http://127.0.0.1:8000`, for the dashboard that is not built yet: register a repository, start an analysis, poll it, then read, edit and approve the profile. It has no login, so it answers only requests made on the same machine; do not expose it. The routes and their limits are in [docs/status.md](docs/status.md#the-api).

`make worker` runs the Temporal worker that executes each analysis the API starts, as a durable workflow with a retry, timeouts and the run's state in the database. To analyse a repository through the API you need both, on top of the database: `make up`, `make migrate`, `make worker` and `make api`. What the worker does and what it leaves out is in [docs/status.md](docs/status.md#the-worker).

The analyzer uses a free model by design, so a run costs nothing. It is rate limited, and the quality of its profiles has not been measured yet. To see or change which model each role uses:

```bash
uv run python config/llm_config.py show     # effective model for every role and where it came from
```

Change a model without touching code: set `LLM_MODEL__REPO_ANALYZER=provider/model`, change a tier with `LLM_MODEL_STRONG`, or edit [config/models.yaml](config/models.yaml). The full development setup (database, Temporal, dashboard) is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Help wanted

The plugin model makes small, well-defined contributions possible:

- **Extractors:** small plugins that read one project file format and give the analyzer hints (a manifest, a build file, a configuration format it does not know yet).
- **Playbooks:** declarative YAML strategies for a product type (developer tool, game, e-commerce, B2B SaaS).
- **Policy packs:** rule bundles for regulated categories (health, finance) and advertising rules.
- **Benchmark:** labelled open-source apps for the repository-understanding benchmark.
- **Review:** challenge the threat model and the safety design. Break it on paper first.

Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the issues labelled `good first issue`.

## Read more

- [Design report (PDF)](docs/design-report/report.pdf): concept, requirements, architecture, LLM configuration, orchestrator console, security, technology choices, roadmap, risks.
- [Implementation status](docs/status.md): what is built, how it was checked, and where the code differs from the design.
- [Diagrams](docs/design-report/figures/): PNG exports; sources are in `docs/design-report/html/`.

Rebuild the report and diagrams:

```bash
cd docs/design-report && tectonic -X compile report.tex     # PDF (needs tectonic)
cd docs/design-report && python3 render.py                  # diagrams (macOS, needs Google Chrome)
```

## Responsible use

OpenMarketer is designed for authentic marketing of your own product. It deliberately does not support fake accounts, fake reviews, purchased engagement, unsolicited bulk replies or messages, or scraping personal data. Official connectors use only documented platform APIs. AI-generated media should be labelled where platforms require it, and voice cloning needs documented consent.

## Things to verify before building on this

- The analyzer has completed full runs against one repository with one free model. Nobody has measured whether its profiles are correct; see [docs/status.md](docs/status.md).
- Model slugs in `config/models.yaml` change as models are released and retired. Only the slugs the analyzer uses have been run against the live API; the embeddings slug has not been checked.
- Statements about platform APIs and about Higgsfield access come from general knowledge. Check current official documentation.
- "OpenMarketer" is a working name.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE).
