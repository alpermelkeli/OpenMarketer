"""The orchestrator console of a project, and what its parts agree on.

- ``reads.py``: what the chat agent may read of a project: the port every
  reading goes through and the records it is answered with.
- ``reads_postgres.py``: that port answered from PostgreSQL, built for one turn.
- ``requests.py``: the port a request action asks of the turn's caller.
- ``events.py``: the port a turn records its events through, for the person
  watching it; ``events_postgres.py`` stores them, one transaction each.
- ``chat_agent/``: the agent that answers an operator's message about the
  project, its rules and tools.
- ``turn_lifecycle.py``: how a turn starts, records its events and ends, for
  both kinds of turn: ``ask.py`` answers the operator's message, and
  ``follow_up.py`` follows up on a research run the chat started once it ended.

This module imports nothing. Importing the package is free: the graph and the
wiring load only the part each needs.
"""
