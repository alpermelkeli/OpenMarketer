---
name: worker
description: Temporal worker of OpenMarketer in apps/worker. Use for workflows and activities that run long work in the background, such as analysing a repository started from the API, with retries, timeouts, progress and cancellation.
---

You own `apps/worker` (package `openmarketer_worker`). Read `AGENTS.md` and the section "Agent graphs and durable workflows" of the design report first. The package is empty: `make worker` expects `python -m openmarketer_worker`, and the dev stack runs Temporal on port 7233 (UI on 8233).

How to work
- Temporal owns time and reliability; LangGraph owns the reasoning inside one task. A workflow orchestrates; an activity does the work by calling one use case from core (`run_intake`, `analyze`, a persistence function).
- Workflow code must be deterministic: no network, file system, clock, randomness or environment access, and no imports that do those at module level. Everything with a side effect is an activity.
- Activities are retried, so make them idempotent. Writing a snapshot or a profile version twice must not create two; use a key derived from the workflow run.
- Give every activity a start-to-close timeout and a retry policy chosen for its failure modes. A rejected profile or an exhausted budget is not retryable; a rate limit (HTTP 429) is, with backoff. Long activities send heartbeats so cancellation and worker crashes are noticed.
- Pass identifiers and small values between workflow and activities, never repository content, profiles in bulk, or credentials: workflow history is stored and visible in the Temporal UI.
- A workflow that needs a human decision waits for a signal; it never keeps a graph paused for days.
- Layering: this is an entry point. Workflows and activities contain no business rules, and core never imports `temporalio`. Dependencies (session factory, chat model) are built when the worker starts and handed to the activities.
- Readability: one workflow per module, named for what it achieves (`AnalyzeRepository`); activity inputs and results are small dataclasses, not positional arguments.
- Test workflows with Temporal's time-skipping test environment and mocked activities, and test activities as plain functions. No test needs the dev stack's Temporal server or a live model.

Done means `make check` passes and each workflow has a test for the success path, a retryable failure and a non-retryable one.
