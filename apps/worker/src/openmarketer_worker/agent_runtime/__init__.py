"""The Temporal half of the agent runtime: running an agent's graph and the actions it asks for.

A graph runs inside one activity until it finishes or asks for an action (see
``openmarketer_core.agent_runtime``). An agent's workflow calls
``run_graph_with_actions``, which runs the graph's steps and, between them,
the actions it asked for, each with its own timeouts and retries. Temporal
decides what runs and how often it is tried again; the graph only decides what
is needed.

- ``vocabulary.py``: names, failure types, fixed texts and policies, as plain
  data that the workflow and the activities share.
- ``workflow_support.py`` and ``driver.py``: what an agent's workflow calls.
- ``workflow_dsl.py``: ``agent_workflow``, which makes a class with optional hooks the
  agent's workflow (workflow code, loaded by the sandbox).
- ``routes.py``: the actions an agent can run: name, payload model and handler.
- ``activity_support.py`` and ``activities.py``: what the worker registers.
- ``agent_worker.py``: ``AgentWorker``, where an agent's handlers are declared and its
  registration is made, with the start-up checks (for the worker's process only).

The workflow sandbox loads this package, so this module imports nothing.
"""
