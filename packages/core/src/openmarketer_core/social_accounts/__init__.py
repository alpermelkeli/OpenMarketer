"""A project's accounts on social platforms: connecting one, listing and disconnecting them,
and publishing a post from one.

- ``catalog.py``: which platforms can be connected, what the installation needs for each,
  and the bounds of what a platform says about an account.
- ``oauth1.py``: signing a request with OAuth 1.0a, which more than one platform uses.
- ``connection.py``: the use cases, and the port each platform's adapter implements.
- ``publishing.py``: the port a platform's publisher implements, its errors, and the rule that
  keeps a retried send from posting twice.
- ``publishing_costs.py``: what a post is reckoned to cost, read from ``config/publishing.yaml``.
- one folder per platform with its adapters: ``x/`` (``authorization.py``, ``publishing.py``).

The rows are in ``db/social_accounts.py`` and ``db/social_connection_attempts.py``.

This module imports nothing.
"""
