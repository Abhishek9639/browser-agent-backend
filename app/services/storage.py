"""Screenshot storage.

Spec section 4.13 requires screenshots to be stored under task-scoped keys and
served only to the task owner. Two things follow from that, and they shape the
whole module:

* **A ref is opaque, never a path.** Callers store what ``save`` returns and
  hand it back to ``load``. They never build one, join one, or see the
  filesystem. That is what lets an S3 implementation drop in later: the ref
  becomes an object key and nothing above this layer changes.
* **A ref is validated, not sanitised.** ``load`` matches against a strict
  pattern and refuses anything else. Stripping ``..`` from a caller-supplied
  string is a game of whack-a-mole; refusing everything that is not
  ``<uuid>/<four digits>.jpg`` is not.

The ref eventually arrives from an HTTP path parameter
(``GET /tasks/{id}/steps/{stepId}/screenshot``), which is why it is treated as
hostile input even though today only our own code produces one.
"""

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.errors import NotFound
from app.core.logging import get_logger

__all__ = [
    "REF_PATTERN",
    "InvalidScreenshotRef",
    "LocalFileStorage",
    "ScreenshotStorage",
    "build_ref",
]

logger = get_logger(__name__)

# <task uuid>/<zero-padded step>.jpg and nothing else. Anchored, so no prefix
# or suffix can smuggle anything past it.
REF_PATTERN: Final = re.compile(
    r"^(?P<task_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/(?P<step>\d{4})\.jpg$"
)
STEP_DIGITS: Final = 4
MAX_STEP_NUMBER: Final = 9999


class InvalidScreenshotRef(NotFound):
    """A ref that does not match the expected shape.

    Deliberately a 404 rather than a 400: a malformed ref and a ref for a
    screenshot that does not exist should be indistinguishable from outside.
    """

    code = "NOT_FOUND"
    default_message = "That screenshot does not exist."


def build_ref(task_id: uuid.UUID, step_number: int) -> str:
    """The canonical ref for a step's screenshot.

    Zero-padded so a directory listing sorts in step order, which makes the
    stored files readable during a demo without a database.
    """
    if not 0 <= step_number <= MAX_STEP_NUMBER:
        raise ValueError(f"step_number must be between 0 and {MAX_STEP_NUMBER}")
    return f"{task_id}/{step_number:0{STEP_DIGITS}d}.jpg"


@runtime_checkable
class ScreenshotStorage(Protocol):
    """Where step screenshots live.

    Local files today, S3 or MinIO later (spec section 2). Callers depend on
    this, never on :class:`LocalFileStorage`.
    """

    async def save(self, task_id: uuid.UUID, step_number: int, data: bytes) -> str: ...

    async def load(self, ref: str) -> bytes: ...

    async def exists(self, ref: str) -> bool: ...

    async def delete_task(self, task_id: uuid.UUID) -> int: ...


class LocalFileStorage:
    """Screenshots on the local filesystem, one directory per task.

    Layout: ``<root>/screenshots/<task_id>/<step>.jpg``. Filesystem calls are
    blocking, so each one is pushed to a worker thread; the agent loop writes a
    screenshot every step and must not stall on disk.
    """

    def __init__(self, settings: Settings) -> None:
        self._root = (Path(settings.STORAGE_DIR) / "screenshots").resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, ref: str) -> Path:
        """Resolve a ref, refusing anything that is not one.

        Two independent checks. The pattern is the real defence: a ref that is
        not ``<uuid>/<digits>.jpg`` never becomes a path at all, so ``..``,
        absolute paths, null bytes, backslashes and URL-encoded separators are
        all rejected by construction. The containment check afterwards is
        belt-and-braces against a symlink inside the tree pointing out of it.
        """
        if not REF_PATTERN.fullmatch(ref):
            logger.warning("screenshot_ref_rejected", reason="malformed")
            raise InvalidScreenshotRef

        candidate = (self._root / ref).resolve()
        if not candidate.is_relative_to(self._root):
            logger.warning("screenshot_ref_rejected", reason="escapes_root")
            raise InvalidScreenshotRef
        return candidate

    async def save(self, task_id: uuid.UUID, step_number: int, data: bytes) -> str:
        ref = build_ref(task_id, step_number)
        path = self._path_for(ref)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename, so a reader never sees a half-written file.
            temporary = path.with_suffix(".jpg.part")
            temporary.write_bytes(data)
            temporary.replace(path)

        await asyncio.to_thread(_write)
        logger.debug("screenshot_saved", step_number=step_number, size_bytes=len(data))
        return ref

    async def load(self, ref: str) -> bytes:
        path = self._path_for(ref)

        def _read() -> bytes:
            if not path.is_file():
                raise InvalidScreenshotRef
            return path.read_bytes()

        return await asyncio.to_thread(_read)

    async def exists(self, ref: str) -> bool:
        try:
            path = self._path_for(ref)
        except InvalidScreenshotRef:
            return False
        return await asyncio.to_thread(path.is_file)

    async def delete_task(self, task_id: uuid.UUID) -> int:
        """Remove every screenshot for a task. Returns how many were deleted.

        Takes a ``UUID``, not a string, so a caller cannot pass a path
        fragment. Idempotent: a task with nothing stored deletes zero.
        """
        directory = (self._root / str(task_id)).resolve()
        if not directory.is_relative_to(self._root):  # pragma: no cover - UUID prevents this
            raise InvalidScreenshotRef

        def _remove() -> int:
            if not directory.is_dir():
                return 0
            count = sum(1 for entry in directory.iterdir() if entry.is_file())
            shutil.rmtree(directory)
            return count

        removed = await asyncio.to_thread(_remove)
        logger.info("screenshots_deleted", task_id=str(task_id), count=removed)
        return removed
