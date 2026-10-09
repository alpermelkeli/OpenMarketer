"""The worker side of the repository analyzer: analysing a project's repository as a workflow.

It holds the ``AnalyzeRepository`` workflow, its activities, the limits they
run under, the settings an operator can change and the removal of checkpoints
a run left behind. It does not hold the analyzer itself (the graph, its rules
and its checkpoint store are in core), does not read the process's own
settings, and is not wired here: ``main.py`` of the worker builds what the
activities need and registers them.

This module imports nothing: the workflow sandbox loads it with ``workflow.py``.
"""
