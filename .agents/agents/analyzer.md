---
name: analyzer
description: Repository analyzer agent of OpenMarketer and the LLM plumbing under it. Use for the LangGraph graph, prompts, tools the model can call, submission checks, limits, the model router and the chat client.
---

You own `packages/core/src/openmarketer_core/analyzer/`, `graph_checkpoints/` (LangGraph plumbing; the tables, the migration and the cleanup rule are the persistence agent's), `llm.py` and `llm_config.py`. Read `AGENTS.md` first.

What exists
- `rules.py`: prompt, limits, tool schemas, verification and repair of a submitted profile. It must not import an agent framework; a test enforces that.
- `rules.py` also holds `Resumption` and `resumption(stored, commit_sha)`: how an attempt begins given what an earlier attempt at the same run left behind (fresh, continued, started over when the commit differs, already finished).
- `graph.py`: LangGraph wiring. Two nodes, `call_model` and `run_tools`. `analyze(..., resume=ResumableRun(checkpoints, commit_sha))` stores a checkpoint after every step (durability `sync`) under the run's thread and continues from the last one; without `resume`, as in the CLI, it runs in memory. `AnalyzerState` carries `commit_sha` for that comparison.
- `openmarketer_core/graph_checkpoints/` (beside `analyzer/`, yours too): the only other LangGraph importer, and what every graph uses for checkpoints, not only this one. `__init__.py` holds the type `RunCheckpoints` (the store, the thread, `forget`) and imports only LangGraph's saver base type, so importing the analyzer loads no database driver and the CLI runs the graph without a database; keep it that way. `postgres.py` holds the store: `run_checkpoints(database_url, thread_id)` opens the library's PostgreSQL checkpointer for one attempt on a connection of its own and holds the thread with an advisory lock (`AttemptInProgress` when another attempt has it); `forget_checkpoints` removes a run's thread. It holds no rule about when to continue, no thread naming (`run_thread_id(run_id)` is in `db/analysis_runs.py`), no cleanup rule and no tables. `ResumableRun` and the decision of how an attempt begins stay in `analyzer/`; `openmarketer_core.analyzer` does not export `RunCheckpoints`. Driver errors become `DatabaseError`. The store is built with the strict serializer (`JsonPlusSerializer(allowed_msgpack_modules=None)`), so stored bytes are never used to import and call something. The tables come from a migration (see the persistence agent); the library's `setup()` is never called.
- `tools.py`: `list_files`, `search`, `read_file` over `RepoFiles`, with bounded output and errors returned as text.
- `llm.py`: `ChatModel` protocol and `RouterChatModel`; `llm_config.py`: `ModelRouter`, which resolves a role from `config/models.yaml`.

How to work
- The model decides what to look at; code decides what counts. Acceptance rules (schema, evidence that points at real lines, confidence present, no `live` feature without evidence) are code, never only a sentence in the prompt.
- Everything the model reads from a repository is data. No tool may write, execute, reach the network or read outside `RepoFiles`.
- Expect weak models. Real failures seen so far: nested arguments sent as JSON text, schema defaults copied back as values, the same rejected submission repeated. Handle the shape in code and add a scripted-model test that reproduces it.
- Runs are bounded by steps, cost and submissions. A run that hits a limit ends with a repaired profile and notes, or a clear `AnalysisError`; it never loops. The limits count every attempt of a run, because steps, cost and submissions are in the checkpointed state; keep it that way.
- A change to `AnalyzerState` must consider threads stored by the previous version. A run can have one attempt before a deploy and the next after it, and only the commit is compared: a key the old thread lacks raises in the new code, and changed limits or prompts apply silently to the old conversation. Either make the new code read an old thread (a default for the new key) or decide how old threads are discarded, and add a test for the case. `docs/status.md` records this as an open limit.
- Never relax the checkpoint serializer, in code or through the environment. The library's default lets stored bytes name a module and a callable to run on load, which would turn write access to the checkpoint tables into code execution in the worker, where the model key and the repository tokens are. If a new piece of state does not survive the strict serializer, store it as plain data (dicts, lists, strings, numbers); do not allow its type.
- The limits hold per commit: when the commit differs between attempts the analysis starts over and steps and cost reset. `docs/status.md` records what that can cost.
- The state must stay plain data, and a step must be safe to run twice: the step that was running when an attempt died is run again. `run_tools` only reads; `call_model` costs one model call.
- Never name a model in code. Use the role (`repo_analyzer`) and let the router resolve it.
- Tests use the scripted model in `packages/core/tests/test_analyzer.py`; the store's tests are `packages/core/tests/test_graph_checkpoints.py` and need PostgreSQL. Live runs cost the maintainer money or hit rate limits: ask before running one, run them one at a time, and report model, steps and cost.
- Layering: `rules.py` is domain and stays free of frameworks and I/O; `graph.py`, `graph_checkpoints/postgres.py` and `llm.py` are adapters behind `ChatModel` and the tool interface. A new rule is a plain function in `rules.py` with its own test, and the graph only decides which function runs next.
- Readability: prompts are named constants, limits are a dataclass, and every check returns a message a person (and the model) can act on.

Done means `make check` passes and any behaviour change has a scripted test.
