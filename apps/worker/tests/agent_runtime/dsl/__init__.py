"""An agent written only with the decorators, split the way an author must split it.

``definitions.py`` is what the workflow may load (no graph framework, no database), ``graph.py``
the nodes (LangGraph), ``handlers.py`` the worker's side, and ``workflow.py`` and
``workflow_passthrough.py`` the workflows. The workflow sandbox imports the workflow modules by
name, so ``conftest.py`` of the folder above puts it on the import path and this is a package.
"""
