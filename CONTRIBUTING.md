# Contributing to OpenMarketer

Thanks for your interest. The project is in early development: the design is written down and the first milestone, the repository analyzer, runs from the command line ([what is built](docs/status.md)). The most valuable contributions right now are small, well-defined pieces and sharp review.

## Ways to help

| Kind | Examples |
|---|---|
| Extractor plugins | A small plugin that reads one project file format (a manifest, a build file) and gives the analyzer hints |
| Playbooks | YAML strategies per product type: channel mix, cadence, KPIs, tone presets |
| Policy packs | CEL rule bundles for regulated categories and advertising rules |
| Benchmark | Candidate open-source apps with a human-labelled Product Profile |
| Review | Threat model, safety design, data model, documentation clarity |
| Tests and docs | More test cases for the model configuration, translations, typo fixes |

Look for issues labelled `good first issue` or `help wanted`. If you have a bigger idea, open an issue first so we can agree on the approach before you invest time.

## Before you start

1. Read the [design report](docs/design-report/report.pdf), at least the sections on architecture, safety and security.
2. Check existing issues and pull requests to avoid duplicate work.
3. For anything that changes behaviour or design, open an issue and describe the problem first.

## Design principles (please keep these)

- **Safety lives outside the model.** Anything that spends money or publishes goes through the deterministic policy gate. Do not move safety rules into prompts.
- **Untrusted text is data.** Comments, messages, web pages and repository files must never be able to cause an action. Keep the reader/actor split.
- **A human approves.** Approval is an authenticated user action, never a statement produced by a model.
- **Official APIs only.** No scraping of private data, no automation that platform terms forbid, no fake accounts or engagement.
- **Everything is configurable and traceable.** Models, budgets and rules are configuration; every action records who or what decided it and why.
- **Keep plugins declarative where possible.** Playbooks and policy packs should be data, not code.

## Development setup (current repository)

You need [uv](https://docs.astral.sh/uv/), Node.js 22+ with pnpm, Docker and [gitleaks](https://github.com/gitleaks/gitleaks).

```bash
cp .env.example .env     # for host-run services use localhost:5433 (Postgres), localhost:7233 (Temporal), localhost:9000 (S3)
make install             # uv sync + pnpm install
make up                  # PostgreSQL + pgvector, Temporal, S3-compatible storage
make check               # lint, type-check, tests (what CI runs)
```

`make help` lists the other targets. `make api` serves the API on http://127.0.0.1:8000 (it needs `make up` and `make migrate`); after changing a route or a request or response model, run `make openapi` and commit `apps/api/openapi.json` together with the dashboard's types it regenerates (`apps/web/src/lib/api/schema.d.ts`). `make web` serves the dashboard on http://localhost:3000 (it needs `make api`). `make worker` runs the Temporal worker (it needs `make up`, `make migrate`, and `TEMPORAL_ADDRESS=localhost:7233` or empty in `.env`, which the API reads too); an analysis started through the API is executed by it, so run both. The workflow tests of the worker and of the API's Temporal adapter use the dev stack's Temporal server and are skipped without it. The Temporal UI is at http://localhost:8233 and the storage console at http://localhost:9001.

To rebuild the report and diagrams you need [tectonic](https://tectonic-typesetting.github.io) and, for the diagram renderer, Google Chrome on macOS. See the README for commands. Diagram sources are plain HTML and CSS in `docs/design-report/html/`; edit the source, re-render the PNG, and commit both.

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and why; link the issue.
- Add or update tests with every change in behaviour; `make check` must pass.
- Update the documentation: `docs/status.md` when what is built changes, and the report source when the design changes.
- Sign off your commits (see below).

### Sign-off (Developer Certificate of Origin)

By contributing you confirm that you have the right to submit your work under the project's license. Add a sign-off line to each commit:

```bash
git commit -s -m "Describe your change"
```

This appends `Signed-off-by: Your Name <you@example.com>`. See https://developercertificate.org for the text of the certificate.

Contributions are licensed under the [Apache License 2.0](LICENSE), the same as the project.

## Commit messages

Use a short imperative summary (for example, `Add dev_tool playbook`), then a blank line and details if needed.

## Reporting security problems

Please do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting for this repository (Security tab, "Report a vulnerability"), or contact the repository owner privately through GitHub.

## Conduct

Be kind and constructive. Critique ideas, not people. Assume good faith, and keep discussions focused on making the project better.
