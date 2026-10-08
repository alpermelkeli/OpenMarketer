# Implementation status

What is built, how it was checked, and where the code differs from the [design report](design-report/report.pdf). The report describes the whole design; this page describes the repository as it is. Last updated 2026-10-08.

## What is built

Phase 1 of the roadmap is "repository analyzer and Product Profile": extractors, secret scan, evidence store, LLM synthesis, review UI and golden-repository evaluation.

| Part | State | Where |
|---|---|---|
| Product Profile schema | Built | `packages/core/src/openmarketer_core/profile.py` |
| Intake: locked-down clone, secret scan, safe file access | Built | `packages/core/src/openmarketer_core/intake/` |
| Extractors (11, as plugins) | Built | `packages/extractors/` |
| Analyzer agent | Built, runs from the CLI | `packages/core/src/openmarketer_core/analyzer/` |
| Model configuration and chat client | Built | `config/models.yaml`, `packages/core/src/openmarketer_core/llm_config.py`, `llm.py` |
| Database models and migrations | Built, but nothing writes to them yet | `packages/core/src/openmarketer_core/db/` |
| Evidence store: saving snapshots, evidence and profile versions | Not built | |
| API | Not built (`apps/api` is an empty package) | |
| Worker | Not built (`apps/worker` is an empty package) | |
| Review UI | Not built (`apps/web` is a scaffold) | |
| Golden-repository evaluation | Not built | |

The one thing that runs end to end is the command line:

```bash
make analyze repo=https://github.com/owner/name
```

It clones the repository, scans and redacts secrets, runs the extractors, lets the analyzer agent explore the files, and prints a draft Product Profile as JSON. Nothing is stored and nothing is published. The profile is a draft: no person has reviewed it.

## How it was checked

- `make check` (ruff, pyright, 197 tests) passes locally and in CI. The tests need no network: model calls are scripted and HTTP uses a fake transport. Database tests run against PostgreSQL, secret-scan tests against gitleaks.
- Intake and the extractors were run by hand on seven public repositories of different kinds (Kotlin Multiplatform, Flutter, Expo, Swift, Rust, Python, Go).
- The analyzer completed full runs against a live model on one repository ([Memoria](https://github.com/alpermelkeli/Memoria), a Kotlin Multiplatform app) with the free model named below. Three runs finished with an accepted profile at no cost; the two whose length was recorded took 12 and 19 model turns and needed no repairs.

What that does not show:

- Whether a profile is correct. A submitted profile is checked for matching the schema and for evidence that points at lines which exist. Nobody has compared a profile with a human-written one.
- Whether it works on other repositories or models. One repository and one model is an example, not a result. Two runs on the same repository produced different feature lists.
- A complete run on a paid model. Earlier attempts on paid models ended at the cost limit of $0.50, or on an account without credit, before a profile was accepted.
- Access to private repositories. The token path exists and is untested against a real private repository.

## Differences from the design report

| Topic | The original design said | The code does | Why |
|---|---|---|---|
| Role of the extractors | Deterministic extractors run first and the model "only synthesises and fills gaps" | The analyzer is an agent that explores the repository itself with read-only tools (`list_files`, `search`, `read_file`). Extractor output is given to it as hints. | Repositories are too varied, and too often monorepos, for extractors to carry the understanding. Covering each stack deterministically did not scale. |
| Files with secrets | A secret scanner runs, and `.env*`, key files and build output are excluded. What happens to a finding inside an ordinary file is not specified; the first implementation excluded the whole file. | The secret is replaced by `[REDACTED]` in place and the file is scanned again; a file is excluded only if a finding cannot be redacted. `.env*`, key files and build output are still excluded outright. | Excluding whole files removed configuration and source files the analyzer needs. |
| Product type and platforms | A fixed list (`consumer_app`, `b2b_saas`, …; `ios`, `android`, …) | Open vocabularies: any lowercase slug is valid, and the listed values are only the well-known ones | A product can be anything: a browser extension, a watch app, a hardware device. |
| Workspace isolation | PostgreSQL row-level security binds a session to one project | Tables carry the identifiers; no row-level security policy exists | Not done yet. |
| Graph checkpoints | Stored in PostgreSQL so a graph can resume | The analyzer graph runs in memory; an interrupted run starts again | Not done yet. |
| Where the analyzer runs | Inside a Temporal activity | Synchronously inside the CLI | The worker is not built. |
| Release tags | Tags and release notes are a source of content | Clones are shallow and fetch no tags | Not needed for the profile; needed later for the repository watcher. |
| Model configuration loader | `config/llm_config.py` | `packages/core/src/openmarketer_core/llm_config.py`; the file in `config/` re-exports it | The core package needs to import it. |
| Extractor list | `flutter, react_native, swift, kotlin, nextjs` | Extractors follow file formats, not frameworks: `package.json`, Gradle, Android manifest, `Info.plist`, Xcode project, Flutter, Expo, Cargo, `pyproject.toml`, `go.mod`, and a generic README and licence reader | Follows from extractors being hints. |

The first three are decisions, and the report text has been updated to match them. The rest are parts of the design that are not built yet. The pipeline diagram in the report (`fig_profile`) still shows the original order and has not been redrawn.

## Model for the analyzer

The `repo_analyzer` role uses the fast tier, and the fast tier is the free model `apodex/apodex-1.1-mini:free`. This is a design decision, described in the report's section on model configuration: analysing a repository costs nothing, and the model's output is checked in code and reviewed by a person. The first version of the design had the role on the strong tier. The quality of profiles from the free model has not been measured; `LLM_MODEL__REPO_ANALYZER` moves the role to another model.

## Model behaviour worth knowing

Weak or free models produced these failures against the analyzer; each is handled in code and covered by a test:

- Nested sections of the profile sent as JSON text instead of objects. They are decoded before validation.
- `confidence` left out, or the schema default `0` copied back. The tool schema shown to the model now requires `confidence` and shows no default, and a submission with a missing confidence, or `0` next to cited evidence, is rejected.
- The same rejected submission sent again. After three submissions, or when the run is out of steps or budget, the profile is repaired in code (bad evidence removed, unevidenced features set to `unknown`) and the corrections are reported. A submission that still does not match the schema at that point ends the run with an error.

Other things observed:

- A request names fallback models. When the primary model fails, OpenRouter serves the next one, which may be paid. The reply states which model answered and the CLI prints it.
- Free models are rate limited upstream (HTTP 429). Runs are best started one at a time.
- Prompt caching is not used.

## What comes next

In order: saving results to the database, the API, the review UI, the golden-repository evaluation. Whether a proposed strategy belongs to Phase 1 is open: the roadmap caption mentions it, the phase description does not.
