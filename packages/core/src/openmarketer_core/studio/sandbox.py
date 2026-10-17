"""What the creative studio asks of the sandbox that runs agent-written code, and what it gets.

The ``Sandbox`` port has three operations: run a Python program the studio's model wrote
(``run_python``, with extra packages installed first when it asks for them), render an HTML
page to a PNG of a given size (``render_html``) and read the text in an image (``ocr``). Each
is given its inputs as bytes, by file name, and returns the image files the run left, its
logs and how long it took; nothing else comes out.

A run that ends badly in the program's own terms (an exception, a non-zero exit, its time
limit) is not an error here: the result says so and carries the logs, so that the model can
fix its code. The errors are for what the caller must handle: ``SandboxRefused`` (screening
refused the code or the packages; nothing ran), ``SandboxFailed`` (the sandbox ran but did
not do its part) and ``SandboxUnavailable`` (there is no sandbox to run in). Their messages are
fixed sentences or screening's reasons, never what the sandbox printed: that goes in ``logs``,
for the audit log.

This module is the port and its data. ``docker_sandbox.py`` is the adapter.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

ImageMime = Literal["image/png", "image/jpeg", "image/webp"]


@dataclass(frozen=True)
class CanvasSize:
    """The size in pixels of the image a run is asked to make."""

    width: int
    height: int


@dataclass(frozen=True)
class SandboxFile:
    """An image a run left in ``/out``, checked: its bytes decode as the type it claims."""

    name: str
    mime: ImageMime
    data: bytes = field(repr=False)
    width: int
    height: int


@dataclass(frozen=True)
class SandboxLogs:
    """What the run printed, cut to a fixed length; untrusted text, for the audit log."""

    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class SandboxResult:
    """The outcome of one run: the images it made, what it printed, how it ended."""

    files: tuple[SandboxFile, ...]
    logs: SandboxLogs
    duration_s: float
    exit_code: int
    timed_out: bool = False
    # Why a file the run left was not accepted (not an image, too large, too many).
    rejected_outputs: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxError(Exception):
    """The sandbox did not run what it was asked to."""


class SandboxRefused(SandboxError):
    """Screening refused what the model asked to run; nothing was started."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("the sandbox refused to run this: " + "; ".join(self.reasons))


class SandboxFailed(SandboxError):
    """The sandbox started but did not do its part; ``logs`` holds what it printed."""

    def __init__(self, message: str, logs: SandboxLogs | None = None) -> None:
        self.logs = logs or SandboxLogs()
        super().__init__(message)


class SandboxUnavailable(SandboxError):
    """There is no sandbox to run in: no container engine, no image, or no network for it."""


class Sandbox(Protocol):
    def run_python(
        self,
        code: str,
        inputs: Mapping[str, bytes],
        requirements: Sequence[str],
        size: CanvasSize,
    ) -> SandboxResult:
        """Run ``code`` as ``/in/main.py`` with ``inputs`` beside it, in ``/out``."""
        ...

    def render_html(
        self, html: str, inputs: Mapping[str, bytes], width: int, height: int
    ) -> SandboxResult:
        """Render ``html`` (as ``/in/page.html``) to a ``width`` by ``height`` PNG."""
        ...

    def ocr(self, image: bytes) -> str:
        """The text an OCR engine reads in ``image`` (PNG, JPEG or WebP)."""
        ...
