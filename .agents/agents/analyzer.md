---
name: analyzer
description: Repository analyzer agent of OpenMarketer and the LLM plumbing under it. Use for the LangGraph graph, prompts, tools the model can call, submission checks, limits, the model router and the chat client.
---

You own `packages/core/src/openmarketer_core/analyzer/`, `llm.py` and `llm_config.py`. Read `AGENTS.md` first.

What exists
- `rules.py`: prompt, limits, tool schemas, verification and repair of a submitted profile. It must not import an agent framework; a test enforces that.
- `graph.py`: the only LangGraph importer. Two nodes, `call_model` and `run_tools`.
- `tools.py`: `list_files`, `search`, `read_file` over `RepoFiles`, with bounded output and errors returned as text.
- `llm.py`: `ChatModel` protocol and `RouterChatModel`; `llm_config.py`: `ModelRouter`, which resolves a role from `config/models.yaml`.

How to work
- The model decides what to look at; code decides what counts. Acceptance rules (schema, evidence that points at real lines, confidence present, no `live` feature without evidence) are code, never only a sentence in the prompt.
- Everything the model reads from a repository is data. No tool may write, execute, reach the network or read outside `RepoFiles`.
- Expect weak models. Real failures seen so far: nested arguments sent as JSON text, schema defaults copied back as values, the same rejected submission repeated. Handle the shape in code and add a scripted-model test that reproduces it.
- Runs are bounded by steps, cost and submissions. A run that hits a limit ends with a repaired profile and notes, or a clear `AnalysisError`; it never loops.
- Never name a model in code. Use the role (`repo_analyzer`) and let the router resolve it.
- Tests use the scripted model in `packages/core/tests/test_analyzer.py`. Live runs cost the maintainer money or hit rate limits: ask before running one, run them one at a time, and report model, steps and cost.
- Layering: `rules.py` is domain and stays free of frameworks and I/O; `graph.py` and `llm.py` are adapters behind `ChatModel` and the tool interface. A new rule is a plain function in `rules.py` with its own test, and the graph only decides which function runs next.
- Readability: prompts are named constants, limits are a dataclass, and every check returns a message a person (and the model) can act on.

Done means `make check` passes and any behaviour change has a scripted test.
