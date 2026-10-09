"""Analysing a repository as one operation of the product, and what its parts agree on.

- ``request.py``: asking for an analysis. The run is stored, then its workflow
  is started; the API calls it.
- ``workflow_contract.py``: the names the API and the worker share to run that
  workflow (its name, its queue, its id and its input). They do not import
  each other, so the names live here.
- ``pipeline.py``: the analysis itself as one call, from a repository URL to a
  draft Product Profile: intake, extractors, then the analyzer agent. The
  worker calls it.
- ``extraction.py``: the interface of the extractor plugins, the deterministic
  step of that pipeline.

- ``intake/``: cloning a repository and reading it safely (the secret scan
  and redaction, ``RepoFiles``, the rule for which host gets a token).
- ``analyzer_agent/``: the agent that drafts the profile, its rules and tools.

What several features share stays above this folder: ``db/`` (run state is in
``db/analysis_runs.py``), ``graph_checkpoints/``, the model access (``llm.py``,
``llm_config.py``), ``profile.py`` and ``workflow_server.py``.

This module imports nothing. The API and the worker's workflow sandbox load it
for the contract, and neither should load the analyzer with it.
"""
