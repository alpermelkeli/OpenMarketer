---
name: evaluation
description: Evaluation of the repository analyzer. Use to build and run the golden-repo benchmark: human-labelled Product Profiles for open-source repositories, scoring of analyzer output against them, and comparison of models or prompt changes.
---

You own `packages/evaluation/`, `openmarketer evaluate` in the CLI (`make evaluate`), the golden cases in `evals/cases/` and the committed results in `evals/results/`. Read `AGENTS.md` first, and "Golden-repository evaluation" in `docs/status.md` for what the benchmark does and what it does not show. There is one case (Excalidraw), with a label written by an AI assistant that no person has reviewed; more cases across stacks and human-reviewed labels are what is missing.

Rules
- A golden case is a public repository pinned to a full commit id plus an expected profile. Never a branch.
- The benchmark runs the analyzer through its own `analyze_repository` in `runner.py`: intake, the extractors and the analyzer's public `analyze`, in memory. That is the same steps as the worker's, not the worker's path: no workflow, no clone action, no checkpoints. Do not reach into the analyzer's graph or the agent runtime, and nothing of the product may import the evaluation (a test checks it).
- Keep loading cases, running and scoring apart. Scoring is pure functions over the expected profile, the draft and the verdicts, one named function per metric with a docstring saying exactly what it counts, so it is tested without a model and reapplied to stored runs.
- Score per field and separately; the costly error is a feature wrongly marked `live`.
- Where a judgement is needed, use the judge, and keep it a different model from the one evaluated. Parse its replies strictly.
- Report distributions, not one run: how many runs a number comes from, with cost and steps beside quality, and the limits of the result first.
- Live runs spend the maintainer's credit or hit rate limits. Ask before running, run sequentially, and keep raw outputs so scoring can be redone without a model call.

Done means the benchmark runs with one command, its scoring code has tests that need no network, and results are written where a later run can be compared against them.
