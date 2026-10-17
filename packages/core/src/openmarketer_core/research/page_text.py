"""The text of a stored page, as a model is shown it.

A page arrives as HTML and is stored as it was fetched; what the reader is
shown is this module's extraction: scripts, styles and markup taken out, and
the rest bounded. Nothing here depends on a model, a database or a network, and
nothing decides what a page may say: extraction is not sanitising, and the
caller treats the result as data.

``PageTextUnavailable`` is raised by whoever reads a stored page when the page
or its stored object is gone; a node that cannot get a page's text leaves the
page out and says so in its notes.
"""

from __future__ import annotations

import html as html_module
import re

# What of one page a reader may be shown. A long page is cut here, not refused.
MAX_PAGE_TEXT_CHARS = 20000
# What of a page's own title is kept; a page without one has an empty title.
MAX_TITLE_CHARS = 200

_STRIP_ELEMENTS = re.compile(r"(?is)<(script|style|noscript|template|svg|head)[^>]*>.*?</\1\s*>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TAG = re.compile(r"(?s)<[^>]*>")
_SPACES = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


class PageTextUnavailable(Exception):
    """The stored page cannot be read: its row or its stored object is gone."""


def plain_text(page: str) -> str:
    """The readable text of an HTML page, bounded to ``MAX_PAGE_TEXT_CHARS``.

    The result is for a model to read and for quotes to be checked against. It
    is not a sanitised version of the page: what it holds is still untrusted
    text, and a caller never runs it, opens it or follows it.
    """
    stripped = _COMMENT.sub(" ", _STRIP_ELEMENTS.sub(" ", page))
    text = html_module.unescape(_TAG.sub(" ", stripped))
    text = _SPACES.sub(" ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return _BLANK_LINES.sub("\n\n", text).strip()[:MAX_PAGE_TEXT_CHARS]


def title_of(page: str) -> str:
    """The page's own ``<title>``, bounded; empty when it has none."""
    found = _TITLE.search(page)
    if found is None:
        return ""
    return html_module.unescape(_SPACES.sub(" ", found.group(1))).strip()[:MAX_TITLE_CHARS]
