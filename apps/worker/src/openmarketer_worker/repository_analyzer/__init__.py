"""The worker side of the repository analyzer: analysing a project's repository as a workflow.

It holds the ``AnalyzeRepository`` workflow, its activities, the limits they
run under, the settings an operator can change and the wiring that builds
what the activities need (``wiring.py``). It does not hold the analyzer
itself (the graph, its rules and its checkpoint store are in core), does not
read the process's own settings, does not clean up checkpoints a run left
behind (the worker does, for every agent), and does not build or serve the
worker: ``main.py`` of the worker registers what the wiring returns.

This module imports nothing: the workflow sandbox loads it with ``workflow.py``.
"""
