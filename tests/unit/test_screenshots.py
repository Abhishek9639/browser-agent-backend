"""B09: screenshot compression (spec section 6.1)."""

import base64
from io import BytesIO

import pytest
from PIL import Image

from app.agent.browser.screenshots import (
    DEFAULT_JPEG_QUALITY,
    MAX_WIDTH,
    Screenshot,
    capture,
    compress,
    to_base64,
)


def png_of(width: int, height: int, *, mode: str = "RGB") -> bytes:
    """A noisy image, so JPEG quality actually changes the size.

    A flat colour compresses to almost nothing at any quality, which would make
    the quality assertions vacuous.

    Built with one ``frombytes`` call rather than a per-pixel comprehension: at
    2560x1600 that is four million Python tuples, which added about 40 seconds
    to the gate.
    """
    import random

    random.seed(width * height)
    channels = 4 if mode == "RGBA" else 3
    image = Image.frombytes(mode, (width, height), random.randbytes(width * height * channels))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class StubPage:
    """The two things capture() needs from a Playwright Page."""

    def __init__(self, raw: bytes, width: int, height: int) -> None:
        self._raw = raw
        self.viewport_size = {"width": width, "height": height}
        self.requested_type: str | None = None

    async def screenshot(self, *, type: str) -> bytes:
        self.requested_type = type
        return self._raw


# ------------------------------------------------------------------ size


def test_a_wide_image_is_downscaled() -> None:
    shot = compress(png_of(2560, 1600), viewport_width=2560, viewport_height=1600)

    assert shot.width == MAX_WIDTH
    assert shot.was_downscaled


def test_aspect_ratio_is_preserved() -> None:
    shot = compress(png_of(2560, 1600), viewport_width=2560, viewport_height=1600)

    assert shot.height == 800  # 1600 * 1280/2560


def test_a_narrow_image_is_left_alone() -> None:
    """Upscaling would add bytes and no information."""
    shot = compress(png_of(800, 600), viewport_width=800, viewport_height=600)

    assert shot.width == 800
    assert not shot.was_downscaled


def test_an_image_exactly_at_the_limit_is_untouched() -> None:
    shot = compress(png_of(MAX_WIDTH, 800), viewport_width=MAX_WIDTH, viewport_height=800)

    assert shot.width == MAX_WIDTH


def test_the_viewport_is_recorded_separately_from_the_image() -> None:
    """The client needs both to map a bbox onto the scaled screenshot."""
    shot = compress(png_of(2560, 1600), viewport_width=1280, viewport_height=800)

    assert (shot.width, shot.height) == (1280, 800)
    assert (shot.viewport_width, shot.viewport_height) == (1280, 800)


# ------------------------------------------------------------------ format


def test_output_is_a_jpeg() -> None:
    shot = compress(png_of(400, 300), viewport_width=400, viewport_height=300)

    with Image.open(BytesIO(shot.jpeg_bytes)) as image:
        assert image.format == "JPEG"


def test_transparency_does_not_break_encoding() -> None:
    """JPEG has no alpha channel; a page with transparency must still encode."""
    shot = compress(png_of(200, 200, mode="RGBA"), viewport_width=200, viewport_height=200)

    with Image.open(BytesIO(shot.jpeg_bytes)) as image:
        assert image.mode == "RGB"


@pytest.mark.parametrize(("low", "high"), [(20, 90), (40, 95)])
def test_lower_quality_means_fewer_bytes(low: int, high: int) -> None:
    raw = png_of(600, 400)

    smaller = compress(raw, viewport_width=600, viewport_height=400, quality=low)
    larger = compress(raw, viewport_width=600, viewport_height=400, quality=high)

    assert smaller.size_bytes < larger.size_bytes


def test_downscaling_saves_a_lot_of_bytes() -> None:
    """The point of the exercise: the live view updates once per action."""
    raw = png_of(2560, 1600)

    full = compress(raw, viewport_width=2560, viewport_height=1600, max_width=2560)
    scaled = compress(raw, viewport_width=2560, viewport_height=1600)

    assert scaled.size_bytes < full.size_bytes / 2


def test_default_quality_matches_the_spec() -> None:
    assert DEFAULT_JPEG_QUALITY == 60
    assert MAX_WIDTH == 1280


# ------------------------------------------------------------------ capture


async def test_capture_asks_for_png_then_compresses() -> None:
    """PNG first: encoding to JPEG then resizing compresses detail twice."""
    page = StubPage(png_of(2560, 1600), 2560, 1600)

    shot = await capture(page)

    assert page.requested_type == "png"
    assert shot.width == MAX_WIDTH


async def test_capture_honours_overrides() -> None:
    page = StubPage(png_of(1600, 900), 1600, 900)

    shot = await capture(page, max_width=400, quality=30)

    assert shot.width == 400


async def test_capture_survives_a_missing_viewport() -> None:
    """Playwright returns None for viewport_size in some headless modes."""
    page = StubPage(png_of(300, 200), 0, 0)
    page.viewport_size = None  # type: ignore[assignment]

    shot = await capture(page)

    assert shot.viewport_width == 0
    assert shot.width == 300


# ------------------------------------------------------------------ base64


def test_to_base64_round_trips() -> None:
    shot = compress(png_of(120, 80), viewport_width=120, viewport_height=80)

    encoded = to_base64(shot)

    assert base64.b64decode(encoded) == shot.jpeg_bytes


def test_to_base64_has_no_data_uri_prefix() -> None:
    """The section 6.1 field is the payload; the client knows it is a JPEG."""
    shot = compress(png_of(40, 40), viewport_width=40, viewport_height=40)

    assert not to_base64(shot).startswith("data:")


def test_screenshot_is_immutable() -> None:
    shot = Screenshot(b"x", 1, 1, 1, 1)

    with pytest.raises(AttributeError):
        shot.width = 2  # type: ignore[misc]
