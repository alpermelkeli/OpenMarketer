"""The product's own images in its repository: which are kept, how they are made safe, collected.

``selection.py`` is the rule (which files, what kind each is, how many are kept),
``preparation.py`` decodes one and writes it again without metadata, and ``collection.py``
runs both over a ``RepoFiles``. A person's own image goes through the same preparation
(``uploads.py``, the rule of an upload) and is stored by ``curation.py``. This package
imports nothing here.
"""
