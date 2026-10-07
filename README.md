# OpenMarketer

**Give it your repository. It markets your app.**

OpenMarketer is an open-source AI agent that reads the source repository of any mobile app or website, builds a verified understanding of the product, and runs the marketing: research, content, publishing, ad campaigns and measurement. It asks you before anything risky or expensive, and it cannot spend beyond the budget you set.

![License](https://img.shields.io/github/license/alpermelkeli/OpenMarketer)

> **Status: design phase.** The architecture, safety model and roadmap are designed and documented. The first code milestone is the repository analyzer. See [what exists today](#status).

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
| Design report: architecture, safety model, data model, security, roadmap | Done ([PDF](docs/design-report/report.pdf), 53 pages) |
| Per-role LLM model configuration through OpenRouter | Reference implementation with tests (`config/`) |
| Repository analyzer and Product Profile | **In progress**: intake, extractors and the analyzer agent run from the CLI; storage, review UI and evaluation are not built yet |
| Content engine and approved publishing (X, Instagram) | Planned |
| Creative studio (compose with code, generate, verify, edit) | Designed |
| Orchestrator console and proactive mode | Designed |
| Ad connectors (recommend-only first) | Planned |
| Plugin SDK and playbook packs | Planned |

Roadmap in one line: Product Profile from any repo, then approved publishing, then community and analytics, then ads, then ecosystem. Details are in the report.

## How it works

<p align="center">
  <img src="docs/design-report/figures/fig_arch.png" alt="System architecture" width="520">
</p>

1. **Understand.** Read-only clone, secret scan, deterministic extractors, LLM synthesis, human review.
2. **Plan and create.** Weekly plan from a playbook; text, images, short video and voice-over through replaceable providers.
3. **Control.** Every action passes the policy engine: banned content, budget caps, content filter, then a risk level that decides between automatic execution and human review.
4. **Act and learn.** Publish through connectors, measure, write results to memory, repeat.

## Try what exists today

The repository currently contains the design documents and the model configuration reference:

```bash
pip install pyyaml pydantic pytest
cp .env.example .env                 # add your OPENROUTER_API_KEY and anything else you need
python config/llm_config.py show     # effective model for every role and where it came from
cd config && pytest                  # 12 tests
```

Change a model without touching code: set `LLM_MODEL__WRITER=provider/model`, change a tier with `LLM_MODEL_STRONG`, or edit [config/models.yaml](config/models.yaml). Model slugs in the file are placeholders; check the current catalogue at https://openrouter.ai/models.

## Help wanted

The plugin model makes small, well-defined contributions possible:

- **Extractors:** teach the analyzer to understand a framework (Flutter, React Native, SwiftUI, Kotlin, Next.js, and so on).
- **Playbooks:** declarative YAML strategies for a product type (developer tool, game, e-commerce, B2B SaaS).
- **Policy packs:** rule bundles for regulated categories (health, finance) and advertising rules.
- **Benchmark:** labelled open-source apps for the repository-understanding benchmark.
- **Review:** challenge the threat model and the safety design. Break it on paper first.

Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the issues labelled `good first issue`.

## Read more

- [Design report (PDF)](docs/design-report/report.pdf): concept, requirements, architecture, LLM configuration, orchestrator console, security, technology choices, roadmap, risks.
- [Diagrams](docs/design-report/figures/): PNG exports; sources are in `docs/design-report/html/`.

Rebuild the report and diagrams:

```bash
cd docs/design-report && tectonic -X compile report.tex     # PDF (needs tectonic)
cd docs/design-report && python3 render.py                  # diagrams (macOS, needs Google Chrome)
```

## Responsible use

OpenMarketer is designed for authentic marketing of your own product. It deliberately does not support fake accounts, fake reviews, purchased engagement, unsolicited bulk replies or messages, or scraping personal data. Official connectors use only documented platform APIs. AI-generated media should be labelled where platforms require it, and voice cloning needs documented consent.

## Things to verify before building on this

- Model slugs in `config/models.yaml` are placeholders in OpenRouter's `provider/model` form.
- The HTTP helpers in `config/llm_config.py` are reference code, not yet run against the live API.
- Statements about platform APIs and about Higgsfield access come from general knowledge. Check current official documentation.
- "OpenMarketer" is a working name.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE).
