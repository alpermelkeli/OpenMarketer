"""The settings a workspace chooses in the dashboard instead of in files or the environment.

- ``model_choices.py``: what can be chosen about models (a tier's model, a
  role's model and parameters, the parameters a model rejects) and the rule
  of a valid choice.
- ``model_settings.py``: which choice is stored, what each tier and role
  resolves to and where that comes from, storing and removing a choice, and
  checking the choices against the provider's catalogue.
- ``analysis_limits.py``: what can be chosen about the limits of an analysis,
  and its bounds.
- ``analysis_settings.py``: the limits in use, storing and removing a choice.
- ``creative_budget.py``: what can be chosen about the budget for generated
  media (the monthly cap, the most one action spends without approval), and
  its bounds.
- ``creative_budget_settings.py``: the caps in use and where each comes from,
  storing and removing a choice.

The rows are in ``db/workspace_settings.py``, with the other stores. The
environment wins over a stored choice wherever it sets the same thing.

This module imports nothing.
"""
