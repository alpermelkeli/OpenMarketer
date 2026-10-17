"""The money a workspace's runs may spend on generated media, and the ledger that holds it.

- ``caps.py``: the caps of a workspace (a month's total, and the most one action
  may spend without a person) and the rule that decides an estimated spend.
- ``ledger.py``: reserving an estimated spend under that rule, then committing
  what was spent or releasing it, and the month's balance for the dashboard.

The rows are in ``db/budget_ledger.py``; where the caps of a workspace come
from is ``workspace_settings/creative_budget_settings.py``.

This module imports nothing.
"""
