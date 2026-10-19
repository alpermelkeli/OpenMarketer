"""What the creative studio asks of a service that makes and edits images, and what it gets.

The ``MediaProvider`` port has three operations: make an image from a prompt (``generate``),
change an image as an instruction says (``edit``), both at an exact size in pixels and with
images to follow as references, and say what one operation is reckoned to cost before it is
made (``estimate``), for a budget to hold. The text in an image is the model's to render: the
prompt says what it reads.

A result is checked before it is returned: its bytes decode as the type it names and it is the
size asked for. What it cost is the provider's figure when the provider reports one.

The errors are what a caller must handle, each a ``MediaProviderError``: ``MediaRefused`` (the
provider refused the content; asking again the same way will be refused again),
``MediaUnavailable`` (the provider could not be reached or was busy; the same request may pass
later), ``MediaAccountLimited`` (the account or the key cannot pay for the request; nothing
passes until a person raises its balance or its limit) and the class itself for anything else
(no image came back, or what came back is not one). Their messages are fixed sentences, never
what the provider wrote: that goes to the log.

This module is the port and its data. ``openrouter_images.py`` is the adapter.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from openmarketer_core.studio.sandbox import ImageMime

MediaOperation = Literal["generate", "edit"]


class MediaProviderError(Exception):
    """The provider did not make the image; the message is a fixed sentence."""


class MediaRefused(MediaProviderError):
    """The provider refused the content of the request: its prompt or one of its images."""


class MediaUnavailable(MediaProviderError):
    """The provider could not be reached, or was too busy; the same request may pass later."""


class MediaAccountLimited(MediaProviderError):
    """The provider's account or key cannot pay for the request: its balance or its usage limit
    is too low. No request passes, whatever it asks, until a person raises it; the provider
    charged nothing for this one."""


@dataclass(frozen=True)
class ImageInput:
    """An image sent to the provider: one to edit, or one to follow as a reference."""

    data: bytes = field(repr=False)
    mime: ImageMime


@dataclass(frozen=True)
class MediaResult:
    """An image the provider made, checked, at the size asked for.

    ``model`` is the one that answered, ``params`` what was asked of it and what was done to
    its answer (plain data, for the audit log), and ``cost_usd`` what it cost.
    """

    data: bytes = field(repr=False)
    mime: ImageMime
    width: int
    height: int
    cost_usd: Decimal
    provider: str
    model: str
    params: Mapping[str, Any]


class MediaProvider(Protocol):
    def generate(
        self, prompt: str, *, width: int, height: int, references: Sequence[ImageInput] = ()
    ) -> MediaResult: ...

    def edit(
        self,
        image: ImageInput,
        instruction: str,
        *,
        width: int,
        height: int,
        references: Sequence[ImageInput] = (),
    ) -> MediaResult: ...

    def estimate(self, operation: MediaOperation) -> Decimal: ...
