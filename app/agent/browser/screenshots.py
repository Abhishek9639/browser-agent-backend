"""Screenshot capture and compression.

Spec section 6.1 puts a base64 JPEG inside every ``step`` WebSocket event, no
wider than 1280 and around quality 60. Those numbers are a bandwidth budget:
the live view updates once per action, and a full-resolution PNG of a retina
viewport is megabytes, which would blow the "≤ ~1-2 s after action" target in
section 9 long before the network did anything wrong.

Pillow does the work rather than Playwright's own JPEG option, because we need
to downscale as well as compress, and Playwright cannot resize.
"""

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Final, Protocol

from PIL import Image

from app.core.logging import get_logger

__all__ = ["DEFAULT_JPEG_QUALITY", "MAX_WIDTH", "Screenshot", "capture", "to_base64"]

logger = get_logger(__name__)

MAX_WIDTH: Final = 1280
DEFAULT_JPEG_QUALITY: Final = 60


class _Screenshotable(Protocol):
    """The slice of a Playwright ``Page`` this module needs.

    Narrow on purpose: it keeps the compression logic testable with a stub and
    keeps Playwright out of the import graph for the pure-image tests.
    """

    async def screenshot(self, *, type: str) -> bytes: ...

    @property
    def viewport_size(self) -> dict[str, int] | None: ...


@dataclass(frozen=True, slots=True)
class Screenshot:
    """A compressed frame, ready for the wire or for storage."""

    jpeg_bytes: bytes
    width: int
    height: int
    viewport_width: int
    viewport_height: int

    @property
    def size_bytes(self) -> int:
        return len(self.jpeg_bytes)

    @property
    def was_downscaled(self) -> bool:
        return self.width < self.viewport_width


def compress(
    raw: bytes,
    *,
    viewport_width: int,
    viewport_height: int,
    max_width: int = MAX_WIDTH,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Screenshot:
    """Downscale if needed, then encode as JPEG.

    Synchronous and CPU-bound; :func:`capture` runs it off the event loop.
    """
    with Image.open(BytesIO(raw)) as image:
        # JPEG has no alpha channel, and a screenshot of a page with
        # transparency would otherwise fail to encode.
        frame = image.convert("RGB")

        if frame.width > max_width:
            height = round(frame.height * max_width / frame.width)
            frame = frame.resize((max_width, height), Image.Resampling.LANCZOS)

        buffer = BytesIO()
        frame.save(buffer, format="JPEG", quality=quality, optimize=True)
        return Screenshot(
            jpeg_bytes=buffer.getvalue(),
            width=frame.width,
            height=frame.height,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )


async def capture(
    page: _Screenshotable,
    *,
    max_width: int = MAX_WIDTH,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Screenshot:
    """Grab the viewport and compress it.

    The capture is PNG because that is lossless, then Pillow resizes and
    encodes once. Encoding straight to JPEG and then resizing would compress
    detail that is about to be thrown away, and compress the artefacts again.
    """
    raw = await page.screenshot(type="png")
    viewport = page.viewport_size or {"width": 0, "height": 0}

    # Pillow is CPU-bound and would block the event loop for a few ms per
    # frame; at one frame per action across concurrent tasks that adds up.
    shot = await asyncio.to_thread(
        compress,
        raw,
        viewport_width=viewport["width"],
        viewport_height=viewport["height"],
        max_width=max_width,
        quality=quality,
    )
    logger.debug(
        "screenshot_captured",
        width=shot.width,
        height=shot.height,
        size_bytes=shot.size_bytes,
    )
    return shot


def to_base64(shot: Screenshot) -> str:
    """The form the section 6.1 ``step`` event carries.

    Plain base64, no data URI prefix: the event field is documented as the
    payload, and the client knows it is a JPEG.
    """
    return base64.b64encode(shot.jpeg_bytes).decode("ascii")
