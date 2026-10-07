# Contributing to OpenMarketer

Thanks for your interest. The project is in its design phase, so the most valuable contributions right now are small, well-defined pieces and sharp review.

## Ways to help

| Kind | Examples |
|---|---|
| Extractor specs and plugins | How to understand a Flutter, React Native, SwiftUI, Kotlin or Next.js repository |
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

```bash
pip install pyyaml pydantic pytest
cd config && pytest
```

To rebuild the report and diagrams you need [tectonic](https://tectonic-typesetting.github.io) and, for the diagram renderer, Google Chrome on macOS. See the README for commands. Diagram sources are plain HTML and CSS in `docs/design-report/html/`; edit the source, re-render the PNG, and commit both.

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and why; link the issue.
- Add or update tests when you change `config/`.
- Update the documentation (and the report source, if the design changes).
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
