---
name: worker
description: Temporal worker of OpenMarketer in apps/worker. Use for workflows and activities that run long work in the background, such as analysing a repository started from the API, with retries, timeouts, progress and cancellation.
---

You own `apps/worker` (package `openmarketer_worker`). Read `AGENTS.md` and the section "Agent graphs and durable workflows" of the design report first. `make worker` runs `python -m openmarketer_worker`, and the dev stack runs Temporal on port 7233 (UI on 8233).

What exists
- `analyze_repository.py`: the `AnalyzeRepository` workflow. It calls three activities by name: `start_run`, `analyse_and_store`, and `record_failure` whenever one of the first two ends without a result (also on a timeout and on cancellation). It imports only `steps.py`, `policy.py` and the contract from core, which the workflow sandbox can load.
- `openmarketer_core/analysis_workflow.py` (in core, no `temporalio`): what the API and the worker share. Workflow name, task queue, `analysis_workflow_id(run_id)` and `AnalyzeRepositoryInput` (workspace, project and run identifiers, nothing else).
- `steps.py`: activity names, the `RunFailure` input, the failure types (`NOT_RETRIED` lists those that end a run at once) and the fixed messages. `policy.py`: every timeout and retry number, with the reasons. `AnalysisPolicy` is configuration and reaches the workflow as the result of `start_run`.
- `activities.py`: `AnalysisActivities(sessions, analyse, policy)`. Each activity calls core and raises only `ApplicationError`s with a type from `steps.py` and a message that may be stored and shown; the original error is logged and not attached as a cause. `analyse_and_store` runs the blocking work in a thread and sends heartbeats.
- `settings.py` and `main.py`: settings from the environment, the wiring (`analysis_with_configured_models`, `build_worker`) and `serve`. Activities read no environment.
- Tests in `apps/worker/tests`: `test_activities.py` (activity environment and PostgreSQL), `test_analyze_repository.py` (the workflow with scripted activities), `test_worker.py` (workflow, real activities and PostgreSQL together, and the wiring), `test_settings.py`. `docs/status.md` lists what the worker does not do, including the failures that leave a run unfinished.

How to work
- Temporal owns time and reliability; LangGraph owns the reasoning inside one task. A workflow orchestrates; an activity does the work by calling one use case from core (`run_intake`, `analyze`, a persistence function).
- Workflow code must be deterministic: no network, file system, clock, randomness or environment access, and no imports that do those at module level. Everything with a side effect is an activity.
- Activities are retried, so make them idempotent. Writing a snapshot or a profile version twice must not create two; use a key derived from the workflow run.
- Give every activity a start-to-close timeout and a retry policy chosen for its failure modes. A rejected profile or an exhausted budget is not retryable; a rate limit (HTTP 429) is, with backoff. Long activities send heartbeats so cancellation and worker crashes are noticed.
- Pass identifiers and small values between workflow and activities, never repository content, profiles in bulk, or credentials: workflow history is stored and visible in the Temporal UI. That includes failures: raise an `ApplicationError` with a chosen message and `from None`, because Temporal stores the message and every cause of whatever an activity raises.
- A workflow that needs a human decision waits for a signal; it never keeps a graph paused for days.
- Layering: this is an entry point. Workflows and activities contain no business rules, and core never imports `temporalio`. Dependencies (session factory, chat model) are built when the worker starts and handed to the activities.
- Readability: one workflow per module, named for what it achieves (`AnalyzeRepository`); activity inputs and results are small dataclasses, not positional arguments.
- Test workflows with scripted activities against the dev stack's Temporal server, on a task queue of the test's own (the `temporal` and `task_queue` fixtures), and skip when it is not reachable. The time-skipping test environment downloads a server on first use, and tests use no network; timeouts in tests are therefore real, so pass a policy with timeouts of a second. Test activities as plain functions in `ActivityEnvironment`. No test needs a live model.

Done means `make check` passes and each workflow has a test for the success path, a retryable failure and a non-retryable one.
