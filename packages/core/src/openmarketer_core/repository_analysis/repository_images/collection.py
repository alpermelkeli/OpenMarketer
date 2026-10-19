"""Collecting the product's own images from an analysed repository.

``collect_repository_images`` lists the repository through ``RepoFiles``, so it sees only
what intake lets later stages read: excluded files (keys, ``.env*``, build output, version
control) and every file in which a secret was found and not blanked out are never opened.
The secret scan reads text; an image is binary, so what it could carry besides its picture
is dropped instead: each kept image is decoded and written again from its pixels alone
(``preparation.py``), and those bytes are what is stored. What the picture itself shows (a
screenshot of a page with a key on it) is not checked.

The rule of which images and how many is ``selection.py``'s. A file that cannot be read or
decoded is skipped and logged by its path and the rule, never its content; nothing here
fails because of one image. It stores nothing: the worker keeps what it answers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openmarketer_core.repository_analysis.intake import ExcludedFileError, RepoFiles
from openmarketer_core.repository_analysis.repository_images.preparation import (
    ImageRefused,
    prepare_image,
)
from openmarketer_core.repository_analysis.repository_images.selection import (
    IMAGE_EXTENSIONS,
    ImageLimits,
    PreparedImage,
    candidates_in_order,
    chosen_images,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectedImages:
    """The images kept, preferred first, and how many candidates were refused on decoding."""

    images: tuple[PreparedImage, ...]
    refused: int


DEFAULT_LIMITS = ImageLimits()


def collect_repository_images(
    files: RepoFiles, limits: ImageLimits = DEFAULT_LIMITS
) -> CollectedImages:
    """The product's images among ``files``, decoded, cleaned and chosen within ``limits``."""
    candidates = candidates_in_order(_image_files(files), limits)
    prepared: list[PreparedImage] = []
    refused = 0
    for candidate in candidates:
        try:
            prepared.append(prepare_image(files.read_bytes(candidate.path), candidate, limits))
        except ImageRefused as e:
            logger.info("repository image %r not kept: %s", candidate.path, e)
            refused += 1
        except (ExcludedFileError, OSError) as e:
            logger.info("repository image %r not read: %s", candidate.path, type(e).__name__)
            refused += 1
    return CollectedImages(images=tuple(chosen_images(prepared, limits)), refused=refused)


def _image_files(files: RepoFiles) -> list[tuple[str, int]]:
    """Each readable file with an image's extension, and its size."""
    found: list[tuple[str, int]] = []
    for path in files:
        if not any(path.lower().endswith(extension) for extension in IMAGE_EXTENSIONS):
            continue
        try:
            found.append((path, files.size_of(path)))
        except (ExcludedFileError, OSError):
            continue
    return found
