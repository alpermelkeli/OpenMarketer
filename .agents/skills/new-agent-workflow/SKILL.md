---
name: new-agent-workflow
description: Add a new product agent to OpenMarketer and run it as a durable Temporal workflow, from the contract in core to the worker, the API and the docs. Use when a new kind of run (a planner, a writer, a publisher) needs its own workflow, or when asked how to add one.
---

# Adding an agent's workflow

A workflow in this project is a product agent run by the agent runtime. Adding one adds an agent, so the work goes through several areas, and each area's owner keeps its rules. This skill gives the order of the work and says who does each step. The rules themselves are in the files below and are not repeated here. Read them first:

- `.agents/agents/worker.md`: the workflow, the sandbox, the hooks, the wiring, the credentials, the history.
- `.agents/agents/analyzer.md`: nodes, actions, state and checkpoints, the model.
- `docs/status.md`: "The worker", "Agent runtime" and "Writing an agent", including the list of what the runtime does not do.
- The greeter in `apps/worker/tests/agent_runtime/dsl/`: a complete agent in four modules (`definitions.py`, `graph.py`, `workflow.py`, `wiring.py`), run by its tests on Temporal and PostgreSQL. The repository analyzer is the real one; copy its shape, not its analysis code.

In the steps below, `<feature>` is the core feature folder (like `repository_analysis/`), `<agent>` is the agent's folder in core (like `analyzer_agent/`), and `<worker agent>` is its folder in the worker (like `repository_analyzer/`).

## 0. Decide before writing code

Write these down in the pull request description:

- **The kind of run.** A slug like `analysis-run`. It names the thread of every checkpoint: `<kind>:<run id>`.
- **What a run produces** and where it is stored. `finish` stores it and returns only an id and a summary.
- **Its actions**, each one with a payload of identifiers only, and whether repeating it is harmless (`attempts=`). An action that **publishes or spends** is declared with `effect=ActionEffect.PUBLISHES` or `SPENDS` (and `approval_wait=` if three days do not fit): the policy gate then holds it for a person's approval, the workflow needs `scope_of` and the registration `sessions=` (`worker.md`, "Rules"). The gate weighs no budget or content yet; if the agent needs that, raise it first.
- **Who starts it**: the API, a schedule or another workflow. Also, which **credentials** it uses.
- **Whether it keeps checkpoints.** Every graph run by the runtime does, so its registration needs the longest a run can take, which bounds the cleanup of its left-over threads (step 7).

## 1. The contract (core, `<feature>/workflow_contract.py`)

Write the names that the starter and the worker share, as plain values with no import of the workflow engine. Use `repository_analysis/workflow_contract.py` as the model:

- the workflow's name;
- a task queue of its own;
- the run kind;
- the input, a frozen dataclass of identifiers (workspace, project, run);
- the function that derives the workflow id from the run id.

No URL, path, content or credential goes in the input.

## 2. The run's record (persistence agent)

Most runs need a table: its status, its timestamps, its error and what it produced. `db/analysis_runs.py` and `AnalysisRunRecord` are the model, including `run_thread_id`.

1. Edit `db/models.py`.
2. Run `make migration m="..."`.
3. Read the generated file before committing it.
4. Write the use cases the hooks call: mark the run started, record a failure, store the result. Keep each one scoped to workspace and project.
5. Write the use case that requests a run. It takes a `start_workflow` callable (`repository_analysis/request.py`).

## 3. The declarations (analyzer agent, `<feature>/<agent>/definitions.py`)

1. Declare the agent: `Agent(KIND, state=..., context=...)`.
2. Declare each action with `@agent.action(name, timeout=..., attempts=...)` on a payload model with `extra="forbid"`.
3. Write the initial state.

The workflow sandbox loads this module, so it imports no LangGraph and no database. The package `__init__` imports nothing.

## 4. The rules and the graph (analyzer agent, `<agent>/rules.py`, `<agent>/graph.py`)

- **Rules go in `rules.py`.** It imports no framework.
- **Nodes go in `graph.py`.** Declare them with `@agent.node`, `agent.edge` and `@agent.route`. Reach LangGraph only through `agent_runtime/langgraph_steps.py` (`request_action`) and `langgraph_agent.py`. Never import it directly.
- **Write a scripted-model test for each behaviour.** Before writing nodes, read the node rules in `analyzer.md`: one action per node, nothing before the call, ask for the same thing again, plain data.
- **A run with no workflow** (CLI or benchmark) runs the graph in memory with `run_from`, as `analyze` does.

## 5. The worker side (worker agent, `apps/worker/src/openmarketer_worker/<worker agent>/`)

Each of the analyzer's modules has a counterpart:

| Module | What it holds |
|---|---|
| `__init__.py` | Nothing: the sandbox loads it |
| `steps.py` | The names of the hooks' activities, the failure types, `NOT_RETRIED`, the worded types, the agent's `FailureTexts`, and the hooks' inputs. All of it enters the history |
| `policy.py` | The `DatabaseWritePolicy` constants of the hooks, the run's limits, and the longest time a run can take (the sum of everything the workflow can spend; the cleanup acts on it) |
| `settings.py` | The agent's settings read from the environment, checked when the worker starts |
| `workflow.py` | `@agent_workflow(agent, name=..., input=..., run_of=..., graph=..., texts=..., worded_types=...)` on a class with no `run`. The hooks call activities by name through `database_write`. Imports go inside `workflow.unsafe.imports_passed_through()`. Never imports `graph.py` or `wiring.py` |
| `activities.py` | The hooks' activities. Each one calls one use case from step 2 and raises `ApplicationError` with a chosen type and `from None` |
| `wiring.py` | `AgentWorker(agent, task_queue=...)`, one `@worker.handler` per action (blocking; `context.action_id` is the idempotency key), `@worker.run_ended` if the run leaves something outside the checkpoints, `AgentSteps(...)`, and `worker.registration(...)`. Importing `graph.py` here is what puts the nodes on the agent |

Use `before`, `after` and `failed` as `worker.md` says. Every way a run can end without a result must reach `failed`, so a run never stays unfinished.

## 6. Serving it (worker agent, `main.py`)

Add the agent's wiring function to `agents` in `main.py`. Each agent gets a `Worker` of its own on its own task queue, and `build_workers` refuses two agents on one queue, so the contract's queue (step 1) must be new. Add the new agent's start-up errors to the ones `serve` words, and a start-up test to `test_worker.py`.

## 7. Removing left-over checkpoints (persistence agent, then worker agent)

Give `registration` the agent's `longest_run` from `policy.py`: everything a run can spend, every attempt and wait of every step and action, and the longest wait for a person of an action that publishes or spends. The registration then adds core's rule for the agent's kind (`remove_stale_checkpoints`), which removes the threads nothing wrote for that long. If the run has a table, a stricter rule like `remove_checkpoints_of_analysis_runs` (`db/checkpoint_cleanup.py`) may be added in core for the new kind's threads only, with its own test, and handed over as a `CheckpointCleanup` from `wiring.py`.

## 8. Credentials (worker agent)

Resolve a credential through `WorkspaceCredentials` inside the activity that uses it, at each attempt. It never goes in state, a checkpoint, an input, a result or a failure. A missing credential ends the run with its error's sentence and is not retried (see `CREDENTIAL_FAILURES` in `repository_analyzer/analysis.py`).

Extend the tests that search the history and the checkpoints for a stored value: the worker's `test_run_credentials.py` and `test_worker.py`. A new kind of credential starts in `credentials/catalog.py`.

## 9. Starting it (api agent; web agent for a screen)

1. Write a Temporal starter like `apps/api/src/openmarketer_api/temporal_workflows.py`. Keep it the only module of the API that imports Temporal: extend it or put a sibling next to it, with `REJECT_DUPLICATE` and no execution timeout.
2. Add a dependency and a route that call the use case from step 2.
3. Run `make openapi`.
4. For a screen, follow `web.md`.

If the agent is started by a schedule or by another workflow instead, write that down in `docs/status.md`.

## 10. Tests

`worker.md` says what "done" means. At minimum:

- **The graph**: scripted-model tests, with no live model.
- **The workflow**: tests on the dev stack's Temporal server, on the test's own task queue, skipped when the server is not reachable. Cover the success path, a retryable failure and a non-retryable one, and check that the history holds identifiers only. `repository_analyzer/test_workflow.py` and the greeter's `test_greeter.py` are the models.
- **The sandbox**: a test that the workflow's module loads no database, no graph framework and no activity code, like the last test of the analyzer's `test_workflow.py`. Extend `agent_runtime/test_boundaries.py` when you add a runtime module.
- **The handlers, activities and wiring**: plain-function tests. A start-up mistake must be refused at start-up.

## 11. Docs and finish

1. Update `docs/status.md`: the row in "What is built", a section for the new agent, and the runtime limits that changed. Record any difference from the design report there, and say so in the pull request.
2. Add the new paths to the `AGENTS.md` layout table. If the agent will grow its own prompts and rules, add an owner file to `.agents/agents/` like `analyzer.md`.
3. Run `make check`. It needs `make up`.
4. Run the `security-review` agent before merging: a new agent adds actions, credentials and a route.
5. If the change touches an existing workflow (the activities it schedules, the hooks, the graph's state or its nodes), say so in the pull request: there is no versioning, and runs in flight may break.
