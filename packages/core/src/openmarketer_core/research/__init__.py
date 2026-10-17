"""Answering questions from public sources as one operation of the product.

- ``request.py``: asking for a research run, and what a request may hold. The
  run is stored, then its workflow is started; the API calls it.
- ``workflow_contract.py``: the names the API and the worker share to run that
  workflow (its name, its queue, its id and its input). They do not import
  each other, so the names live here.
- ``failures.py``: what a research run raises when it ends without a report.

- ``research_agent/``: the agent that answers the questions, its rules and its
  graph. Nothing of it publishes or spends: the pages it reads are public, and
  the search and the rendering behind its actions are the installation's own.

- ``web_search.py``: asking an installation's SearXNG instance for results.
- ``page_fetch.py``: fetching a public page safely (HTTPS only, robots.txt,
  size and type limits, no private addresses, nothing behind a login).
- ``page_render.py``: rendering a page with a headless browser when plain HTTP
  returns a script shell; speaks the Chrome DevTools Protocol to Lightpanda.
- ``page_text.py``: what of an HTML page a model is shown, and the signs that
  a page tries to instruct it.
- ``page_store.py``: keeping fetched pages in the installation's object
  storage until they expire; a report keeps only the quotes it relied on.

What several features share stays above this folder: ``db/`` (run state is in
``db/research_runs.py``), ``graph_checkpoints/``, the model access (``llm.py``,
``llm_config.py``) and ``workflow_server.py``.

This module imports nothing. The API and the worker's workflow sandbox load it
for the contract, and neither should load the agent with it.
"""
