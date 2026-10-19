"""A person's own image, uploaded to a project's repository images: the rule it must meet.

An upload is as untrusted as a repository's file, so it goes through the same preparation
(``preparation.py``): its first bytes must be those of a PNG, a JPEG or a WebP, it must decode
as that one format with one frame and a size in pixels within the selection's bounds, and what
is stored is written again from its pixels alone, without its metadata. Before that, an upload
of more than ``MAX_UPLOAD_BYTES`` is refused undecoded. Its name is the person's text: only
its last part is kept, cleaned to letters, digits and a few marks, and bounded, because it is
stored and shown.

How many uploads a project keeps is ``MAX_PERSON_IMAGES``; the store holds it to that. Pure over
bytes and text; storing is ``curation.py``'s.
"""

from __future__ import annotations

import re
import unicodedata

from openmarketer_core.repository_analysis.repository_images.preparation import prepare_image
from openmarketer_core.repository_analysis.repository_images.selection import (
    ImageCandidate,
    ImageLimits,
    PreparedImage,
    RepositoryImageKind,
)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PERSON_IMAGES = 24
MAX_UPLOAD_NAME_LENGTH = 120
# What a name is called when nothing of it is left after cleaning.
UNNAMED = "image"

# The bounds of a repository's images, and an upload's own size limit for its stored bytes.
UPLOAD_LIMITS = ImageLimits(max_file_bytes=MAX_UPLOAD_BYTES)

_NOT_KEPT = re.compile(r"[^A-Za-z0-9._ -]+")
_SPACES = re.compile(r"\s+")


class UploadTooLarge(Exception):
    """The upload is larger than an upload may be."""

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"an upload is at most {MAX_UPLOAD_BYTES} bytes, this one is {size_bytes}")


def upload_name(name: str) -> str:
    """``name`` as it is stored: its last part, cleaned and bounded, never empty."""
    last = re.split(r"[/\\]", name)[-1]
    ascii_name = unicodedata.normalize("NFKD", last).encode("ascii", "ignore").decode("ascii")
    cleaned = _SPACES.sub(" ", _NOT_KEPT.sub("_", ascii_name)).strip(" ._")
    return cleaned[:MAX_UPLOAD_NAME_LENGTH].rstrip(" ._") or UNNAMED


def prepare_upload(name: str, data: bytes, kind: RepositoryImageKind) -> PreparedImage:
    """The upload decoded and written again, as the image to store.

    Raises ``UploadTooLarge``, and ``ImageRefused`` (``preparation.py``) for what is not kept
    as an image, with a message that names the rule.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadTooLarge(len(data))
    candidate = ImageCandidate(path=upload_name(name), kind=kind)
    return prepare_image(data, candidate, UPLOAD_LIMITS)
