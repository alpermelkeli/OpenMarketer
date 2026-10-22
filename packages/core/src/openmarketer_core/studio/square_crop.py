"""What a square picture keeps when it is cropped to a platform's shape, and the sentence that
tells an image model so.

Some image models return a square for some of the shapes they are asked for, and the adapter
crops the square about its centre to the size's proportions (``openrouter_images.fit_to_size``).
What
survives is the central region of the target's shape: for 4:5 the middle 80% of the width at full
height, for 16:9 the full width and the middle 56% of the height. An image model told so keeps
the words and the important elements inside it, and draws no border or band around it because
that region is the whole image (``crop_safe_note``).

The note is meant for every target that is not square, whichever model answers: a model that
keeps the shape it was asked for loses nothing to it but calmer edges, and which model returns
squares for which shape is learnt only from its replies, after the first one was already
cropped.

This module reaches no image library, no network and no database.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# How far a shape may be from square and still be kept whole by a central crop.
SQUARE_TOLERANCE = 0.01

CROP_SAFE_LEAD = "The picture may be delivered square and cropped to its central "


@dataclass(frozen=True)
class CentralCrop:
    """The share of a square's width and of its height that a crop to a shape keeps, each in
    (0, 1]; the kept region is centred."""

    width_share: float
    height_share: float

    @property
    def keeps_the_whole_square(self) -> bool:
        return min(self.width_share, self.height_share) >= 1 - SQUARE_TOLERANCE


def central_crop_of_a_square(width: int, height: int) -> CentralCrop:
    """What a square keeps when cropped about its centre to ``width`` by ``height``: the full
    side along the target's longer side, and the target's ratio of the other."""
    if width <= 0 or height <= 0:
        raise ValueError("a size has a positive width and height")
    if width >= height:
        return CentralCrop(width_share=1.0, height_share=height / width)
    return CentralCrop(width_share=width / height, height_share=1.0)


def crop_safe_note(width: int, height: int) -> str:
    """The sentence an image model is told for a ``width`` by ``height`` image, or ``""`` for a
    square one: the share is rounded down, so what it is told to keep is inside the crop."""
    crop = central_crop_of_a_square(width, height)
    if crop.keeps_the_whole_square:
        return ""
    if crop.width_share < crop.height_share:
        region = f"{_percent(crop.width_share)}% of the width"
    else:
        region = f"{_percent(crop.height_share)}% of the height"
    return (
        CROP_SAFE_LEAD
        + region
        + ": that central region is the whole image, so keep every word, card, icon and "
        "important element inside it. Anything outside it is cut away; never draw a border, "
        "frame or band around it."
    )


def _percent(share: float) -> int:
    return math.floor(share * 100)
