---
name: extractors
description: Extractor plugins of OpenMarketer in packages/extractors. Use to add or fix deterministic extractors that read manifests and project files (package.json, Gradle, Xcode, Flutter, Expo, Cargo, pyproject, go.mod and others) and emit facts as hints for the analyzer.
---

You own `packages/extractors` and the interface in `packages/core/src/openmarketer_core/repository_analysis/extraction.py`. Read `AGENTS.md` first.

Rules
- An extractor is a small class matching the `Extractor` protocol. It reads through `RepoFiles` and returns `Fact`s, each with the file and line it came from. Register it under the `openmarketer.extractors` entry point group in `packages/extractors/pyproject.toml`, then `uv sync`. It imports the interface from core and nothing else of the project; core discovers extractors and never imports one by name.
- Extractors produce hints, not conclusions. Report what the file states and stop; the analyzer is the reader.
- Deterministic and repeatable: the worker runs the extractors more than once on the same commit in one analysis (for the model's hints and again for the stored evidence) and expects the same facts.
- Never fail the run: malformed or surprising input yields no facts. Never execute anything from the repository or evaluate a build script; parse it as text.
- Repositories are often monorepos. Facts are grouped by project folder (`group_by_scope`), folders such as examples and docs are marked auxiliary, and paths stay relative to the repository root.
- One file format per module, parsing apart from fact building, fact kinds named like the existing ones. Each extractor has a test file in `packages/extractors/tests` with small inline fixtures, including one malformed input and one monorepo layout.

Done means `make check` passes and the new extractor appears in `discover_extractors()`.
