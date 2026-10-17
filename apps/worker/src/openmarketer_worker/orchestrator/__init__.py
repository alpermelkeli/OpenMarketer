"""The orchestrator's work in the worker: the follow-up turn of a chat after a research run.

The operator's own turns are answered in the API's process. What runs here is the turn the
orchestrator takes on its own once a research run that a chat started has ended:

- ``contract.py``: the workflow's name, task queue, input and id, which the ``Research``
  workflow starts it with;
- ``workflow.py``: the ``ChatFollowUp`` workflow, one activity and nothing else;
- ``activities.py``: that activity, which resolves the workspace's model and runs core's
  ``follow_up_on_research``;
- ``steps.py``: the activity's name, its failure types and the sentences of its failures;
- ``policy.py``: how long the activity may take and how long it is tried;
- ``wiring.py``: building it, and what the process registers.

This module imports nothing: the workflow sandbox loads it with the workflow.
"""
