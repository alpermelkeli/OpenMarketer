# OpenMarketer

A source-available, repository-driven autonomous marketing agent (working name). Free for noncommercial use; commercial use needs the owner's permission.

Give it the repository of any mobile app or website. It builds a verified understanding of the product, researches the market, creates content (text, images, short video, voice-over), publishes it, runs ad campaigns inside hard budget limits, measures results and learns. You stay in control through approvals, budget caps, an autonomy ladder and a kill switch.

## Status

**Design phase.** This repository currently holds the design documentation and a small reference for the LLM model configuration. There is no application code yet.

## Repository layout

```
.
├── README.md
├── .env.example                  all environment variables, with comments
├── .gitignore
├── config/
│   ├── models.yaml               per-role model configuration (OpenRouter)
│   ├── llm_config.py             reference loader and router for models.yaml
│   └── test_llm_config.py        tests for the loader (12 tests)
└── docs/
    └── design-report/
        ├── report.pdf            the design report (English, 47 pages)
        ├── report.tex            LaTeX source
        ├── figures/              diagrams as PNG
        ├── html/                 diagram sources (HTML + CSS + a small JS helper)
        └── render.py             renders html/*.html to figures/*.png
```

## The design report

Read [docs/design-report/report.pdf](docs/design-report/report.pdf). It covers:

- concept and requirements, system architecture, data model
- LLM access through OpenRouter with a configurable model per role
- repository analysis and the Product Profile
- safety: approval levels, budget controls, autonomy ladder
- the orchestrator console (dashboard chat) and proactive mode (digests, reminders, questions, alerts)
- security and trust model (prompt-injection defence, reader/actor split)
- platform and media integrations (fal.ai for images, Higgsfield for video, ElevenLabs for voice)
- technology selection with alternatives and reasons
- licensing and community strategy, deployment, evaluation
- a commercial path: installing and operating the system for companies
- a twelve-month roadmap, risk register and open questions

## Key ideas

- **Generic by construction.** The repository is analysed into a structured, versioned, human-approved Product Profile. Strategy comes from replaceable playbooks, and platforms, media providers and frameworks are plugins.
- **Safety outside the model.** Budget caps, approval rules and content filters are deterministic code at the tool boundary. Untrusted text goes through a quarantined reader agent that has no tools.
- **Chat prepares, a human approves.** The orchestrator can read, prepare and propose. Only an authenticated click approves anything.
- **A model per role.** Every LLM call site is a named role whose model can be changed through environment variables, dashboard settings or `models.yaml`.

## LLM model configuration

Roles, tiers, fallbacks and routing preferences live in [config/models.yaml](config/models.yaml). Resolution order, first match wins:

1. environment variable `LLM_MODEL__<ROLE>`
2. project setting (dashboard)
3. workspace setting (dashboard)
4. `models.yaml`: the role's model
5. `models.yaml`: the role's tier (`LLM_MODEL_FAST`, `LLM_MODEL_BALANCED`, `LLM_MODEL_STRONG` override a tier)
6. `models.yaml`: the default model

Try it:

```bash
pip install pyyaml pydantic pytest
cp .env.example .env            # then fill in OPENROUTER_API_KEY and anything else you need
python config/llm_config.py show
cd config && pytest
```

## Rebuilding the report and diagrams

```bash
# PDF (needs tectonic: https://tectonic-typesetting.github.io)
cd docs/design-report && tectonic -X compile report.tex

# Diagrams (macOS, needs Google Chrome; the path is set in render.py)
cd docs/design-report && python3 render.py            # all diagrams
cd docs/design-report && python3 render.py fig_arch   # one diagram
```

## Things to verify before building on this

- The model slugs in `config/models.yaml` are placeholders in OpenRouter's `provider/model` form. Check the current catalogue at https://openrouter.ai/models.
- The HTTP functions in `llm_config.py` (`complete`, `fetch_catalog`) are reference code and have not been run against the live API. The resolution logic and request building are covered by tests.
- Statements about platform APIs (Instagram, X, TikTok, LinkedIn, Google Ads) and about Higgsfield API access come from general knowledge and must be checked against current official documentation.
- "OpenMarketer" is a working name and needs a trademark check before any public release.

## License

Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). In short:

- You may read, run, modify and share the code for **noncommercial** purposes: personal study and research, hobby projects, and use by charitable, educational, public-research, public-safety or government organisations.
- You may **not** use it commercially without the owner's written permission. That includes building a commercial product or service from it, offering it as a service, and using it in a business.
- This is a source-available licence, **not** an OSI-approved open-source licence.

### Commercial use

To use OpenMarketer commercially (for example, installing it for a company, offering it as a service or including it in a product), you need a separate commercial licence from the owner. Open an issue in this repository or contact the repository owner on GitHub.

### Contributions

Contributions will require a contributor licence agreement (CLA) so that they can be included in commercially licensed distributions. The CLA process is not set up yet; please open an issue before sending a pull request.
