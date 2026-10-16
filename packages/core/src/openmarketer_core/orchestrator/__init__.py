"""The orchestrator console of a project, and what its parts agree on.

- ``reads.py``: what the chat agent may read of a project: the port every
  reading goes through and the records it is answered with.
- ``reads_postgres.py``: that port answered from PostgreSQL, built for one turn.
- ``chat_agent/``: the agent that answers an operator's message about the
  project, its rules and tools.

This module imports nothing. Importing the package is free: the graph and the
wiring load only the part each needs.
"""
