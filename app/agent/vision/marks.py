"""Set-of-marks annotation (spec section 4.9).

Draws a numbered box over every candidate element so a vision model can answer
"which one" with a number instead of coordinates. Numbers are the whole point:
a model asked for pixel coordinates is being asked to be precise about
something it is bad at, while a model asked to read a label off a picture is
being asked to do the one thing it is reliably good at.

Everything here is deterministic and offline. Nothing in this module calls a
model, so a badly drawn frame is reproducible from the same inputs.
"""

import io
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from app.agent.perception.dom_extractor import ElementInfo
from app.core.logging import get_logger

__all__ = [
    "MAX_MARKS",
    "Mark",
    "MarkedImage",
    "annotate",
]

logger = get_logger(__name__)

# Wider than this and the labels stop being legible once the provider
# downscales it again at their end.
DEFAULT_MAX_WIDTH: Final = 1024
DEFAULT_QUALITY: Final = 70

# Past this the picture is a wall of numbers and the model does worse than the
# DOM summary it was meant to help with.
MAX_MARKS: Final = 40

# High contrast against both light and dark pages. Orange fill with white
# text is legible on a white form and on a dark hero image, which a red or a
# blue box is not.
BOX_OUTLINE: Final = (255, 78, 0)
BOX_WIDTH: Final = 3
LABEL_FILL: Final = (255, 78, 0)
LABEL_TEXT: Final = (255, 255, 255)
LABEL_PADDING: Final = 3
LABEL_HEIGHT: Final = 18


@dataclass(frozen=True, slots=True)
class Mark:
    """One numbered box, and the element it stands for."""

    label: int
    element_index: int
    x: int
    y: int
    width: int
    height: int

    @property
    def centre(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class MarkedImage:
    """The annotated frame and what the numbers on it mean."""

    jpeg_bytes: bytes
    width: int
    height: int
    marks: tuple[Mark, ...]
    scale: float

    @property
    def size_bytes(self) -> int:
        return len(self.jpeg_bytes)

    def mark_for(self, label: int) -> Mark | None:
        return next((mark for mark in self.marks if mark.label == label), None)


def annotate(
    screenshot: bytes,
    elements: list[ElementInfo],
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = DEFAULT_QUALITY,
    max_marks: int = MAX_MARKS,
) -> MarkedImage:
    """Draw numbered boxes over the candidates and return the frame.

    Labels start at 1 rather than at the element's own index, because the
    element indices on a page are sparse once hidden elements are withheld and
    a model reading "3, 17, 41" off a picture has to work harder than one
    reading "1, 2, 3". The mapping back is kept on the marks.
    """
    with Image.open(io.BytesIO(screenshot)) as frame:
        image = frame.convert("RGB")

        scale = 1.0
        if image.width > max_width:
            scale = max_width / image.width
            image = image.resize(
                (max_width, max(1, round(image.height * scale))), Image.Resampling.LANCZOS
            )

        candidates = _candidates(elements, image.width, image.height, scale, max_marks)
        canvas = ImageDraw.Draw(image)
        font = _font()

        for mark in candidates:
            _draw(canvas, mark, font, image.width)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)

    marked = MarkedImage(
        jpeg_bytes=buffer.getvalue(),
        width=image.width,
        height=image.height,
        marks=tuple(candidates),
        scale=scale,
    )
    logger.debug(
        "marks_drawn",
        marks=len(marked.marks),
        width=marked.width,
        size_bytes=marked.size_bytes,
    )
    return marked


def _candidates(
    elements: list[ElementInfo],
    width: int,
    height: int,
    scale: float,
    max_marks: int,
) -> list[Mark]:
    """Which elements get a box, in reading order.

    Only what is on screen: a box drawn for an element below the fold would
    land on whatever happens to be at those coordinates, which is worse than
    not drawing it. Reading order because a model scanning a page reads the
    same way, so the numbers run down the picture rather than around it.
    """
    visible = [
        element
        for element in elements
        if element.in_viewport and element.bbox.width > 0 and element.bbox.height > 0
    ]
    visible.sort(key=lambda item: (item.bbox.y, item.bbox.x))

    marks: list[Mark] = []
    for label, element in enumerate(visible[:max_marks], start=1):
        box = element.bbox
        x = max(0, min(width - 1, round(box.x * scale)))
        y = max(0, min(height - 1, round(box.y * scale)))
        w = max(1, min(width - x, round(box.width * scale)))
        h = max(1, min(height - y, round(box.height * scale)))
        marks.append(Mark(label=label, element_index=element.index, x=x, y=y, width=w, height=h))
    return marks


def _draw(
    canvas: ImageDraw.ImageDraw,
    mark: Mark,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
) -> None:
    canvas.rectangle(
        [mark.x, mark.y, mark.x + mark.width, mark.y + mark.height],
        outline=BOX_OUTLINE,
        width=BOX_WIDTH,
    )

    text = str(mark.label)
    label_width = LABEL_PADDING * 2 + 7 * len(text)

    # Above the box by preference, inside it when that would go off the top,
    # and shifted left when it would go off the right edge. Two labels can
    # still land on each other on a dense page, but neither ends up outside
    # the picture, which is the failure that makes a mark unreadable.
    left = mark.x
    top = mark.y - LABEL_HEIGHT
    if top < 0:
        top = mark.y
    if left + label_width > width:
        left = max(0, width - label_width)

    canvas.rectangle([left, top, left + label_width, top + LABEL_HEIGHT], fill=LABEL_FILL)
    canvas.text((left + LABEL_PADDING, top + 2), text, fill=LABEL_TEXT, font=font)


def _font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Whatever Pillow can supply without anything installed on the host."""
    return ImageFont.load_default()
