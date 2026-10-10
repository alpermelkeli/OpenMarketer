---
name: evaluation
description: Evaluation of the repository analyzer. Use to build and run the golden-repo benchmark: human-labelled Product Profiles for open-source repositories, scoring of analyzer output against them, and comparison of models or prompt changes.
---

You own the evaluation of OpenMarketer's analyzer. Read `AGENTS.md` first. The benchmark exists: `packages/evaluation/` (cases, runner, judge, scoring, results, report), `openmarketer evaluate` in the CLI (`make evaluate`), golden cases in `evals/cases/` and committed results in `evals/results/`. `docs/status.md` (Golden-repository evaluation) says what it does and what it does not show. There is one case so far (Excalidraw), whose label was written by an AI assistant and not reviewed by a person; more cases across stacks and human-reviewed labels are what is missing.

How to work
- A golden case is a public repository pinned to a commit plus a human-written expected profile. Pin the commit; a moving branch makes scores meaningless.
- Score what matters to the next steps, per field and separately: product name, type and platforms; features found and missed; features wrongly marked `live` (the costly error, since only `live` features may appear in public content); evidence that actually supports the claim; and whether confidence tracks correctness.
- Scoring is deterministic where it can be. Where a judgement is needed (does this feature match that one?), use the `judge` role and keep it a different model from the one being evaluated.
- Report distributions, not one run. Output varies between runs and models; say how many runs a number comes from, and report cost and steps next to quality.
- State the limits of a result: a handful of repositories of one kind does not show the analyzer works on arbitrary ones. Cover different stacks and shapes (mobile, web, CLI, library, monorepo).
- Live runs spend the maintainer's credit or hit rate limits. Ask before running, run sequentially, and cache raw outputs so scoring can be redone without new model calls.
- Layering: keep three parts apart: loading cases, running the analyzer (through its public `analyze` function, never its internals), and scoring. Scoring is pure functions over two profiles, so it is tested without a model and reused on cached outputs.
- Readability: each metric is one named function with a docstring stating exactly what it counts.

Done means the benchmark runs with one command, its scoring code has tests that need no network, and results are written somewhere a later run can be compared against.
