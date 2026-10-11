"""What an agent graph and the workflow that runs it agree on, so a graph can ask for side effects.

A graph runs inside one activity until it finishes or needs the outside world:
an e-mail, an API call, heavy work. It does not do that itself. A node asks
for an action and the graph pauses; the workflow checks the request against a
list of allowed actions, runs it as an activity of its own, and gives the
outcome back to the same graph thread, which continues where it paused.

- ``actions.py``: the request, the outcome and the catalog that says which
  actions exist and what their payload may hold.
- ``steps.py``: what one run of a graph returns (finished, or waiting for an
  action), how a run is named, and why a step cannot be taken.
- ``threads.py``: the name of the thread a run keeps its checkpoints under.
- ``langgraph_steps.py``: the LangGraph side: ``request_action`` for node
  code, and ``GraphThread``, which takes one step of a graph on a thread.

Everything but ``langgraph_steps.py`` is plain values and rules, with no
framework: the worker's workflow sandbox and the API load this package, and
neither should load LangGraph with it. Which activity runs an action, the
retries, the approval and the history belong to the worker, not here.

This module imports nothing.
"""
