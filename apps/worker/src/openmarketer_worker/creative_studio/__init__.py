"""The creative studio: the agent that makes one image, checks it and edits it, run by the worker.

- ``workflow.py``: the ``CreativeStudio`` workflow, which is the studio's agent with three
  hooks; the API and the chat's request action start it.
- ``activities.py``: the three activities the hooks call.
- ``studio.py``: what the runtime's activities do for a run: the attempt's context (its
  model, its limits, the brand kit, the run's stored images), the handlers of the five
  actions, the estimate of a paid image, storing the outcome.
- ``steps.py``: the names of the hooks' activities, the failure types and the sentences a
  run is told.
- ``policy.py``: how long each part of a run may take, how often it is tried, and the
  longest a run can last.
- ``settings.py``: the environment an operator sets (the run's limits, the sandbox).
- ``wiring.py``: building all of it, and what the process registers.

This module imports nothing: the workflow sandbox loads it with the workflow.
"""
