"""The researcher: the agent that answers questions from public sources, run by the worker.

- ``workflow.py``: the ``Research`` workflow, which is the researcher's agent
  with two hooks; the API starts it.
- ``activities.py``: the two activities the hooks call.
- ``research.py``: what the runtime's activities do for a run: the attempt's
  context (its model and the stored pages), the search and the fetch handlers,
  storing the report.
- ``steps.py``: the names of the hooks' activities, the failure types and the
  sentences a run is told.
- ``policy.py``: how long each part of a run may take and how often it is
  tried.
- ``settings.py``: the environment an operator sets (its limits, the search
  and rendering addresses, the retention of stored pages).
- ``wiring.py``: building all of it, and what the process registers.
- ``retention.py``: the schedule that removes the pages a run fetched once
  they expire.

This module imports nothing: the workflow sandbox loads it with the workflow.
"""
