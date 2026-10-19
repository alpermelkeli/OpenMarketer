"""An agent with an action that spends: it generates an image, under the creative budget.

Split as an author splits an agent (``definitions.py``, ``graph.py``, ``workflow.py``,
``wiring.py``), like the announcer. Its tests run it on Temporal and PostgreSQL, with the
budget's caps from the environment it is given, and decide its proposals the way the API does.
"""
