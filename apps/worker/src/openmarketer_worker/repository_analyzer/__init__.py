"""The worker side of the repository analyzer: analysing a project's repository as a workflow.

It holds the ``AnalyzeRepository`` workflow (``workflow.py``), the two
activities of its hooks (``activities.py``), what the agent runtime's
activities do for a run of the analyzer's graph (``analysis.py``), the clones
of the runs on this worker's disk (``clones.py``), the repository tokens a
clone may carry for its run's workspace (``repository_tokens.py``), the limits
all of it runs under (``policy.py``), the settings an operator can change (``settings.py``)
and the wiring that builds it (``wiring.py``). It does not hold the analyzer
itself (the agent, its graph, its rules and its checkpoint store are in core)
or the runtime that takes a graph's steps (``agent_runtime/``), does not read
the process's own settings, does not resolve a credential (the worker does,
for every agent: ``credentials.py`` above this folder), does not clean up
checkpoints a run left behind (the worker does, for every agent), and does not
build or serve the worker:
``main.py`` of the worker registers what the wiring returns.

This module imports nothing: the workflow sandbox loads it with ``workflow.py``.
"""
