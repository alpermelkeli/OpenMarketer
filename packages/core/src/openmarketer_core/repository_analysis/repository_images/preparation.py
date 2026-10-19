"""Decoding one image of a repository and writing it again with nothing but its pixels.

A repository's file is untrusted: its extension may lie, it may decode to far more pixels
than its bytes suggest, and it may carry text that is not its picture (EXIF and XMP, PNG text
chunks, an ICC profile, bytes after the image's end). ``prepare_image`` keeps a file only when
its first bytes are those of a PNG, a JPEG or a WebP (``studio/output_check.py``'s
``sniffed_format``), Pillow reads it as that format, it has one frame, and its size in pixels
read from the header, before anything is decoded, is within the selection's bounds. It is
then turned upright by its EXIF orientation and written again from its pixels alone: a PNG
when it has transparency or was a PNG, a WebP otherwise. Nothing of the original file but its
pixels is in what is stored.

What is refused is answered with a reason that names the rule, never the content. Pure over
bytes; the module imports Pillow and no other framework.
"""

from __future__ import annotations

import hashlib
import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from openmarketer_core.repository_analysis.repository_images.selection import (
    ImageCandidate,
    ImageLimits,
    PreparedImage,
    StoredImageMime,
    size_refusal,
)
from openmarketer_core.studio.output_check import sniffed_format

PNG: StoredImageMime = "image/png"
WEBP: StoredImageMime = "image/webp"
WEBP_QUALITY = 90


class ImageRefused(Exception):
    """The file is not kept as an image; the message names the rule it broke."""


def prepare_image(data: bytes, candidate: ImageCandidate, limits: ImageLimits) -> PreparedImage:
    """``data`` decoded and written again as the image to store. Raises ``ImageRefused``."""
    announced = sniffed_format(data)
    if announced is None:
        raise ImageRefused("the file is not a PNG, JPEG or WebP image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[announced]) as image:
                refusal = size_refusal(*image.size, limits)
                if refusal is not None:
                    raise ImageRefused(refusal)
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageRefused("animated images are not kept")
                image.load()
                pixels = _pixels_only(ImageOps.exif_transpose(image) or image)
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ImageRefused("the file does not decode as the image it claims to be") from None

    mime: StoredImageMime = PNG if announced == "PNG" or pixels.mode == "RGBA" else WEBP
    encoded = _encoded(pixels, mime)
    if len(encoded) > limits.max_file_bytes:
        raise ImageRefused(f"written again it is larger than {limits.max_file_bytes} bytes")
    return PreparedImage(
        candidate=candidate,
        data=encoded,
        mime=mime,
        width=pixels.width,
        height=pixels.height,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _pixels_only(image: Image.Image) -> Image.Image:
    """A new image of the same pixels, in RGB or RGBA, with none of the file's metadata."""
    transparent = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    converted = image.convert("RGBA" if transparent else "RGB")
    fresh = Image.new(converted.mode, converted.size)
    fresh.paste(converted)
    return fresh


def _encoded(image: Image.Image, mime: StoredImageMime) -> bytes:
    out = io.BytesIO()
    if mime == PNG:
        image.save(out, format="PNG", optimize=True)
    else:
        image.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()
