"""The secrets an installation can set from the dashboard instead of the environment.

- ``catalog.py``: which credentials exist, the environment variable of each,
  the rule for an acceptable value and the shape of a sealed one.
- ``cipher.py``: sealing a value under the installation's ``ENCRYPTION_KEY``
  and opening it again.
- ``resolution.py``: the value to use for a credential, where each one comes
  from, and setting or removing a stored one.

The rows are in ``db/credentials.py``, with the other stores.

This module imports nothing.
"""
