"""A project's accounts on social platforms: connecting one, listing and disconnecting them.

- ``catalog.py``: which platforms can be connected, what the installation needs for each,
  and the bounds of what a platform says about an account.
- ``oauth1.py``: signing a request with OAuth 1.0a, which more than one platform uses.
- ``connection.py``: the use cases, and the port each platform's adapter implements.
- one folder per platform with its adapter: ``x/``.

The rows are in ``db/social_accounts.py`` and ``db/social_connection_attempts.py``.

This module imports nothing.
"""
