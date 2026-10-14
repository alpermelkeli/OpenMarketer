"""An agent with an action the policy gate holds for a person: it publishes an announcement.

Split as an author splits an agent (``definitions.py``, ``graph.py``, ``workflow.py``,
``wiring.py``), like the greeter of ``dsl``. Its tests run it on Temporal and PostgreSQL and
decide its proposals the way the API does: in the database, then with a signal.
"""
