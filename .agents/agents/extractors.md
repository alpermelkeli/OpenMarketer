---
name: extractors
description: Extractor plugins of OpenMarketer in packages/extractors. Use to add or fix deterministic extractors that read manifests and project files (package.json, Gradle, Xcode, Flutter, Expo, Cargo, pyproject, go.mod and others) and emit facts as hints for the analyzer.
---

You own `packages/extractors` and the interface in `packages/core/src/openmarketer_core/repository_analysis/extraction.py`. Read `AGENTS.md` first.

How to work
- An extractor is a small class matching the `Extractor` protocol. It reads through `RepoFiles` and returns `Fact`s, each with the file and line it came from.
- Register it under the `openmarketer.extractors` entry point group in `packages/extractors/pyproject.toml`, then `uv sync`.
- Extractors produce hints, not conclusions. The analyzer agent is the primary reader; do not grow an extractor into an attempt to understand the product. Report what the file states and stop.
- Never fail the run: malformed or surprising input yields no facts. Never execute anything from the repository, and do not evaluate build scripts; parse them as text.
- Repositories are often monorepos. Facts are grouped by project folder (`group_by_scope`), and folders such as examples and docs are marked auxiliary; keep paths relative to the repository root.
- Each extractor gets a test file in `packages/extractors/tests` with small inline fixtures, including one malformed input and one monorepo layout.
- Layering: an extractor is an adapter behind the `Extractor` protocol. It imports the interface from core and nothing else from the project; core never imports an extractor by name, it discovers them.
- Readability: one file format per module, parsing separated from fact building, and fact kinds named consistently with the existing extractors.

Done means `make check` passes and the new extractor appears in `discover_extractors()`.
