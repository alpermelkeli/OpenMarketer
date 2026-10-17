"""Which of the files a sandbox run left are accepted as its images.

What a run writes to ``/out`` was made by agent-written code, so it is untrusted until it is
checked here: a file is kept only when its first bytes are those of a PNG, a JPEG or a WebP,
Pillow reads it as that same format, it has one frame, and it is within the limits on bytes and
pixels. The size in pixels is read from the header before anything is decoded, so a small file
that would decode to a huge image is refused before it costs memory. The run as a whole is held
to a number of files and a total of bytes.

What is refused is dropped with a reason that names the file and the rule, never its content.
This module decides nothing about what an image shows; that is the verifier's.
"""

import io
import re
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from openmarketer_core.studio.sandbox import ImageMime, SandboxFile

# A file name as the sandbox reports it: a plain name, no directory, nothing hidden.
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


@dataclass(frozen=True)
class OutputLimits:
    max_files: int = 8
    max_file_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 40 * 1024 * 1024
    max_pixels: int = 40_000_000
    max_side: int = 10_000


@dataclass(frozen=True)
class RawOutput:
    """A file the run left, as read back from the sandbox."""

    name: str
    data: bytes


@dataclass(frozen=True)
class OutputCheck:
    files: tuple[SandboxFile, ...]
    rejected: tuple[str, ...]


_FORMATS: dict[str, ImageMime] = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def sniffed_format(data: bytes) -> str | None:
    """The image format the first bytes announce, among those accepted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def check_outputs(outputs: Sequence[RawOutput], limits: OutputLimits) -> OutputCheck:
    """Keep the outputs that are images within the limits; say why each other one is not."""
    accepted: list[SandboxFile] = []
    rejected: list[str] = []
    total = 0
    for output in outputs:
        if len(accepted) >= limits.max_files:
            rejected.append(f"{_shown(output.name)}: more than {limits.max_files} files")
            continue
        if total + len(output.data) > limits.max_total_bytes:
            rejected.append(
                f"{_shown(output.name)}: the run's files exceed {limits.max_total_bytes} bytes"
            )
            continue
        file, reason = check_image(output, limits)
        if file is None:
            rejected.append(f"{_shown(output.name)}: {reason}")
            continue
        accepted.append(file)
        total += len(output.data)
    return OutputCheck(tuple(accepted), tuple(rejected))


def check_image(output: RawOutput, limits: OutputLimits) -> tuple[SandboxFile | None, str]:
    """``output`` as an accepted image, or ``None`` and why it is refused."""
    if not _FILE_NAME.fullmatch(output.name):
        return None, "the file name is not a plain name"
    if len(output.data) > limits.max_file_bytes:
        return None, f"the file is larger than {limits.max_file_bytes} bytes"
    announced = sniffed_format(output.data)
    if announced is None:
        return None, "the file is not a PNG, JPEG or WebP image"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(output.data), formats=[announced]) as image:
                width, height = image.size
                if width > limits.max_side or height > limits.max_side:
                    return None, f"the image is larger than {limits.max_side} pixels a side"
                if width * height > limits.max_pixels:
                    return None, f"the image has more than {limits.max_pixels} pixels"
                if getattr(image, "n_frames", 1) != 1:
                    return None, "animated images are not accepted"
                image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return None, "the file does not decode as the image it claims to be"
    except Image.DecompressionBombWarning:
        return None, f"the image has more than {limits.max_pixels} pixels"
    file = SandboxFile(output.name, _FORMATS[announced], output.data, width, height)
    return file, ""


def _shown(name: str) -> str:
    return name if _FILE_NAME.fullmatch(name) else "<unnamed file>"
