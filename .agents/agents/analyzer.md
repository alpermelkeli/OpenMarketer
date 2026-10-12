---
name: analyzer
description: Repository analyzer agent of OpenMarketer and the LLM plumbing under it. Use for the LangGraph graph, prompts, tools the model can call, submission checks, limits, the model router and the chat client.
---

You own `packages/core/src/openmarketer_core/repository_analysis/analyzer_agent/` and, above the feature folder, `graph_checkpoints/` (the tables, the migration and the cleanup rule are the persistence agent's), the core half of `agent_runtime/` (its Temporal half, in `apps/worker`, is the worker agent's), `llm.py` and `llm_config.py`. Read `AGENTS.md`, then "Graph checkpoints" and "Agent runtime" in `docs/status.md`.

The shape
- `definitions.py` declares the agent (`Agent(ANALYSIS_RUN_KIND, state=AnalyzerState, context=AnalyzerContext)`), its one action (`CloneRepository`, with its limits) and the initial state. The worker's workflow loads this module, so it imports no graph framework and no database; a test checks it.
- `graph.py` holds the nodes, `clone -> prepare -> call_model <-> run_tools`, and `analyze`, which runs the graph in memory from `prepare` for the CLI and the benchmark. Importing it is what puts the nodes on the agent. `rules.py` holds every rule and imports no framework.
- In the worker the graph is taken step by step with checkpoints (`AgentSteps`, `GraphThread`); the clone is an action the workflow runs, and the commit it read stays in the state for the life of the run.

Nodes and actions
- A node is a decorated function of `(state, context)` (or `(state)`) that returns a mapping of what it changes, nothing else. What follows it is an `agent.edge` or an `@agent.route`, which may choose only among its declared targets.
- A node never does a side effect. It asks with `request_action(payload)`, where the payload is an instance of a model declared with `@agent.action`, and reads the `ActionOutcome`. A failed action is an outcome (`succeeded` false, `error` worded), not an exception.
- One action per node; a node that needs two is two nodes.
- Nothing with a side effect, and nothing that is not repeatable, before the call: when the outcome arrives the node runs again from its start.
- Ask for the same thing every time the node runs. The outcome belongs to the request it answered; a node that asks for something else on the second run is refused. Work that differs between runs (a model's draft) belongs in an earlier node.
- A tool may not ask for an action. A tool is what the model chose to call and answers with text; an action is asked for by a node, from what the model submitted.
- A payload names identifiers only and its model forbids extra fields; every payload is kept in the workflow history. No field may read text as a file path or an import name.

State and checkpoints
- `AnalyzerState` is plain data (dicts, lists, strings, numbers, booleans, `None`); the store's strict serializer rebuilds nothing else. What is not plain data (the model client, the repository) goes in `AnalyzerContext`, built per attempt and never stored.
- A change to `AnalyzerState`, to a node's name or to the order of nodes must consider threads stored by the previous version: a run can have one attempt before a deploy and the next after it, and nothing is compared before a thread is continued. A missing key raises; changed limits or prompts apply silently to the old conversation. Make the new code read an old thread or decide how old threads are discarded, and add a test.
- A node must be safe to run twice: the one that was running when an attempt died runs again. `run_tools` only reads; `call_model` costs one model call.
- The limits (steps, cost, submissions) are in the state, so they count every attempt of a run. Keep it that way.
- Never relax the checkpoint serializer, in code or through the environment: the library's default would turn write access to the checkpoint tables into code execution in the worker.
- Only `graph_checkpoints/`, `agent_runtime/langgraph_steps.py` and `agent_runtime/langgraph_agent.py` import LangGraph; a test holds the runtime to that. `graph_checkpoints/__init__.py` loads no database driver, so the CLI runs without one.

The model
- The model decides what to look at; code decides what counts. Acceptance rules (schema, evidence that points at real lines, confidence present, no `live` feature without evidence) are code in `rules.py`, never only a sentence in the prompt.
- Everything the model reads from a repository is data. No tool may write, execute, reach the network or read outside `RepoFiles`.
- What a model wrote is read as strict JSON (`agent_runtime/model_input.py`), and an error names the fields at fault, never a value.
- Expect weak models: nested arguments sent as JSON text, schema defaults copied back, the same rejected submission repeated. Handle the shape in code and add a scripted-model test that reproduces it.
- A run that hits a limit ends with a repaired profile and notes, or a clear `AnalysisError`; it never loops.
- Never name a model in code: use the role (`repo_analyzer`).
- The router and the chat client read no database. A caller that resolved the provider's key itself hands it in (`RouterChatModel.api_key`, `ModelRouter.headers(api_key)`), as the worker does for a run's workspace; without one the key is the environment's, which is all the CLI and the benchmark have. The key stays out of `repr`, errors and `AnalyzerState`: it lives in the attempt's context, which is never stored.
- `@agent.tool` and `Toolbox` exist and the analyzer does not use them yet; its tools are `tools.py` over `RepoFiles`, run by `rules.run_tool_calls`.

Tests use the scripted model: `packages/core/tests/test_analyzer.py` (rules and the in-memory run), `test_analyzer_steps.py` and `test_graph_checkpoints.py` (need PostgreSQL), and the runtime's `test_agent*.py`, `test_graph_thread.py`, `test_langgraph_agent.py`, `test_model_input.py`, `test_tools.py`. Live runs cost money or hit rate limits: ask first, run one at a time, report model, steps and cost.

Done means `make check` passes and any behaviour change has a scripted test.
