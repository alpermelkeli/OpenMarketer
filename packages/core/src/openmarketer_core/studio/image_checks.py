"""The programmatic checks of a studio candidate: its file against its platform, its words.

``check_image`` reads an image's header and measures it against the platform's
``PlatformSpec``: its size and aspect ratio, its bytes and its format. ``copy_matches``
compares the text an OCR engine read in the image with the exact lines the request gave, after
the same normalisation of both (case, Unicode forms, punctuation, whitespace), and says of each
line that is not there whether it is missing or reads differently (misspelled), with what was
read in its place. Code measures these exactly; a model is poor at noticing a wrong letter.

What is not checked here: the platform's safe margin. The OCR the sandbox gives reads text
without positions, so whether the text keeps clear of the edges is the visual verifier's
question for now. Contrast and the distance from the palette are not measured either.

Every function is pure, over bytes and text; Pillow reads the header and nothing else is
decoded, and it is loaded only when an image is checked. Nothing here decides what a failure
leads to: that is ``studio_agent/rules.py``.
"""

from __future__ import annotations

import difflib
import io
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from openmarketer_core.studio.platforms import PlatformSpec

# The failures a check names; the decide rule reads them as keys.
WRONG_SIZE = "size"
WRONG_FORMAT = "format"
TOO_MANY_BYTES = "bytes"
UNREADABLE = "unreadable"
COPY = "copy"

# How close a stretch of the read text must be to a line to count as that line misspelled
# rather than missing.
MISSPELLED_AT = 0.6
# The most of the read text a report repeats for one line.
MAX_READ_CHARS = 160

_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_NOT_WORD = re.compile(r"[^\w]+")


@dataclass(frozen=True)
class ImageCheck:
    """How an image measures against its platform; ``failures`` is empty when it fits."""

    width: int
    height: int
    size_bytes: int
    mime: str
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Misspelled:
    """A line of copy the image shows differently: what it should read, and what was read."""

    expected: str
    read: str


@dataclass(frozen=True)
class CopyCheck:
    """Whether every line of copy is in the read text, and what of each is not."""

    missing: tuple[str, ...] = ()
    misspelled: tuple[Misspelled, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return not self.missing and not self.misspelled

    def as_data(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "missing": list(self.missing),
            "misspelled": [{"expected": m.expected, "read": m.read} for m in self.misspelled],
        }


def check_image(data: bytes, spec: PlatformSpec) -> ImageCheck:
    """Measure ``data`` against ``spec``: its size in pixels, its bytes and its format."""
    # Imported here, not with the module: the studio's rules name this module's failure keys,
    # and the workflow that loads the rules has no use for Pillow.
    from PIL import Image, UnidentifiedImageError

    from openmarketer_core.studio.output_check import sniffed_format

    announced = sniffed_format(data)
    if announced is None:
        return ImageCheck(0, 0, len(data), "", (UNREADABLE,))
    try:
        with Image.open(io.BytesIO(data), formats=[announced]) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return ImageCheck(0, 0, len(data), "", (UNREADABLE,))
    mime = _MIMES[announced]
    failures: list[str] = []
    if (width, height) != (spec.width, spec.height):
        failures.append(WRONG_SIZE)
    if mime not in spec.formats:
        failures.append(WRONG_FORMAT)
    if len(data) > spec.max_bytes:
        failures.append(TOO_MANY_BYTES)
    return ImageCheck(width, height, len(data), mime, tuple(failures))


def normalized(text: str) -> str:
    """``text`` as copy is compared: one Unicode form, folded case, words and single spaces."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NOT_WORD.sub(" ", folded).split())


def copy_matches(ocr_text: str, exact_copy: Sequence[str]) -> CopyCheck:
    """Whether each line of ``exact_copy`` is in ``ocr_text``, compared normalised.

    The read text is taken as one run of words, because OCR breaks lines where the image does
    and not where the copy does. A line that is not in it is misspelled when a stretch of as
    many words reads close to it, and missing otherwise.
    """
    read = normalized(ocr_text)
    read_words = read.split()
    missing: list[str] = []
    misspelled: list[Misspelled] = []
    for line in exact_copy:
        wanted = normalized(line)
        if not wanted or _contains(read, wanted) or _contains_through_noise(read_words, wanted):
            continue
        closest, ratio = _closest_stretch(read_words, wanted)
        if ratio >= MISSPELLED_AT:
            misspelled.append(Misspelled(line, closest[:MAX_READ_CHARS]))
        else:
            missing.append(line)
    return CopyCheck(tuple(missing), tuple(misspelled))


def _contains(read: str, wanted: str) -> bool:
    return f" {wanted} " in f" {read} "


# OCR reads specks, scribbles and textures as short tokens ("o", "ae") that land between the
# words of a line. Between two words of a line, up to this many such tokens are passed over;
# a word of the line itself must still be read exactly.
NOISE_TOKENS_BETWEEN_WORDS = 2
NOISE_TOKEN_MAX_CHARS = 3


def _contains_through_noise(read_words: list[str], wanted: str) -> bool:
    """Whether the words of ``wanted`` are read in order, with at most a few short tokens of
    noise between two of them."""
    words = wanted.split()
    for start, word in enumerate(read_words):
        if word != words[0]:
            continue
        position, matched = start + 1, 1
        while matched < len(words):
            skipped = 0
            while (
                position < len(read_words)
                and read_words[position] != words[matched]
                and len(read_words[position]) <= NOISE_TOKEN_MAX_CHARS
                and skipped < NOISE_TOKENS_BETWEEN_WORDS
            ):
                position, skipped = position + 1, skipped + 1
            if position >= len(read_words) or read_words[position] != words[matched]:
                break
            position, matched = position + 1, matched + 1
        if matched == len(words):
            return True
    return False


def _closest_stretch(read_words: list[str], wanted: str) -> tuple[str, float]:
    """The stretch of the read words nearest ``wanted``, of its length give or take one word."""
    count = len(wanted.split())
    best, best_ratio = "", 0.0
    for size in {max(1, count - 1), count, count + 1}:
        for start in range(max(1, len(read_words) - size + 1)):
            stretch = " ".join(read_words[start : start + size])
            ratio = difflib.SequenceMatcher(None, stretch, wanted).ratio()
            if ratio > best_ratio:
                best, best_ratio = stretch, ratio
    return best, best_ratio
