"""B09: screenshot storage, including the traversal guard."""

import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.storage import (
    InvalidScreenshotRef,
    LocalFileStorage,
    ScreenshotStorage,
    build_ref,
)


@pytest.fixture
def storage(tmp_path: Path, settings: Settings) -> LocalFileStorage:
    return LocalFileStorage(settings.model_copy(update={"STORAGE_DIR": str(tmp_path)}))


@pytest.fixture
def task_id() -> uuid.UUID:
    return uuid.uuid4()


# ------------------------------------------------------------------ contract


def test_local_storage_satisfies_the_protocol(storage: LocalFileStorage) -> None:
    """Callers depend on the protocol, so S3 can replace this later."""
    assert isinstance(storage, ScreenshotStorage)


# ------------------------------------------------------------------ refs


def test_ref_shape(task_id: uuid.UUID) -> None:
    assert build_ref(task_id, 7) == f"{task_id}/0007.jpg"


def test_step_numbers_are_zero_padded_so_listings_sort(task_id: uuid.UUID) -> None:
    refs = [build_ref(task_id, n) for n in (1, 2, 10, 100)]

    assert refs == sorted(refs)


@pytest.mark.parametrize("step", [-1, 10_000])
def test_out_of_range_steps_are_refused(task_id: uuid.UUID, step: int) -> None:
    with pytest.raises(ValueError, match="step_number"):
        build_ref(task_id, step)


async def test_a_ref_is_not_an_absolute_path(storage: LocalFileStorage, task_id: uuid.UUID) -> None:
    """Callers store the ref; they must never see the filesystem."""
    ref = await storage.save(task_id, 1, b"jpeg")

    assert not Path(ref).is_absolute()
    assert str(storage.root) not in ref


# ------------------------------------------------------------------ round trip


async def test_save_then_load(storage: LocalFileStorage, task_id: uuid.UUID) -> None:
    ref = await storage.save(task_id, 3, b"fake-jpeg-bytes")

    assert await storage.load(ref) == b"fake-jpeg-bytes"


async def test_save_is_scoped_per_task(storage: LocalFileStorage) -> None:
    """Spec section 4.13: task-scoped keys."""
    first, second = uuid.uuid4(), uuid.uuid4()

    a = await storage.save(first, 1, b"one")
    b = await storage.save(second, 1, b"two")

    assert a != b
    assert await storage.load(a) == b"one"
    assert await storage.load(b) == b"two"


async def test_saving_the_same_step_twice_overwrites(
    storage: LocalFileStorage, task_id: uuid.UUID
) -> None:
    await storage.save(task_id, 1, b"first")
    ref = await storage.save(task_id, 1, b"second")

    assert await storage.load(ref) == b"second"


async def test_no_partial_file_is_left_behind(
    storage: LocalFileStorage, task_id: uuid.UUID
) -> None:
    """Written to a temporary name then renamed, so a reader never sees half."""
    await storage.save(task_id, 1, b"data")

    leftovers = list((storage.root / str(task_id)).glob("*.part"))

    assert leftovers == []


async def test_exists(storage: LocalFileStorage, task_id: uuid.UUID) -> None:
    ref = await storage.save(task_id, 1, b"data")

    assert await storage.exists(ref)
    assert not await storage.exists(build_ref(uuid.uuid4(), 1))


async def test_loading_a_missing_screenshot_is_404(
    storage: LocalFileStorage, task_id: uuid.UUID
) -> None:
    with pytest.raises(InvalidScreenshotRef) as caught:
        await storage.load(build_ref(task_id, 99))

    assert caught.value.http_status == 404


# ------------------------------------------------------------------ traversal


@pytest.mark.parametrize(
    "ref",
    [
        "../../../../etc/passwd",
        "../secrets.jpg",
        "/etc/passwd",
        "/absolute/0001.jpg",
        "..%2F..%2Fetc%2Fpasswd",
        "....//....//etc/passwd",
        "00000000-0000-0000-0000-000000000000/../../../etc/passwd",
        "00000000-0000-0000-0000-000000000000/0001.jpg/../../../etc/passwd",
        "00000000-0000-0000-0000-000000000000\\0001.jpg",
        "not-a-uuid/0001.jpg",
        "00000000-0000-0000-0000-000000000000/1.jpg",
        "00000000-0000-0000-0000-000000000000/0001.png",
        "00000000-0000-0000-0000-000000000000/0001.jpg.exe",
        "00000000-0000-0000-0000-000000000000/0001.jpg\x00.txt",
        " 00000000-0000-0000-0000-000000000000/0001.jpg",
        "",
    ],
)
async def test_hostile_refs_are_refused(storage: LocalFileStorage, ref: str) -> None:
    """Validated against a strict pattern, not sanitised.

    Stripping "??" from caller input is whack-a-mole; refusing everything that
    is not <uuid>/<four digits>.jpg is not. The ref reaches us from an HTTP
    path parameter, so it is hostile input by default.
    """
    with pytest.raises(InvalidScreenshotRef):
        await storage.load(ref)


async def test_a_hostile_ref_reports_the_same_error_as_a_missing_one(
    storage: LocalFileStorage, task_id: uuid.UUID
) -> None:
    """Otherwise the difference tells an attacker their probe was well-formed."""
    with pytest.raises(InvalidScreenshotRef) as malformed:
        await storage.load("../../etc/passwd")
    with pytest.raises(InvalidScreenshotRef) as missing:
        await storage.load(build_ref(task_id, 42))

    assert malformed.value.code == missing.value.code
    assert malformed.value.http_status == missing.value.http_status


async def test_exists_is_false_rather_than_raising_for_a_hostile_ref(
    storage: LocalFileStorage,
) -> None:
    assert not await storage.exists("../../etc/passwd")


async def test_nothing_escapes_the_root(storage: LocalFileStorage, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"secret")

    with pytest.raises(InvalidScreenshotRef):
        await storage.load(f"../../{outside.name}")

    assert outside.read_bytes() == b"secret"


# ------------------------------------------------------------------ delete


async def test_delete_task_removes_everything_for_that_task(
    storage: LocalFileStorage, task_id: uuid.UUID
) -> None:
    for step in range(1, 4):
        await storage.save(task_id, step, b"data")

    removed = await storage.delete_task(task_id)

    assert removed == 3
    assert not await storage.exists(build_ref(task_id, 1))


async def test_delete_task_leaves_other_tasks_alone(storage: LocalFileStorage) -> None:
    doomed, kept = uuid.uuid4(), uuid.uuid4()
    await storage.save(doomed, 1, b"one")
    survivor = await storage.save(kept, 1, b"two")

    await storage.delete_task(doomed)

    assert await storage.load(survivor) == b"two"


async def test_delete_task_is_idempotent(storage: LocalFileStorage, task_id: uuid.UUID) -> None:
    assert await storage.delete_task(task_id) == 0
    assert await storage.delete_task(task_id) == 0


async def test_a_symlink_out_of_the_root_is_refused(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    """The second layer of the guard, and the only case that reaches it.

    The pattern rejects every malformed ref, so containment is unreachable
    through input alone. A symlink planted inside the tree is the way a
    well-formed ref can still resolve outside it.
    """
    secret_dir = tmp_path.parent / "elsewhere"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "0001.jpg").write_bytes(b"secret")

    planted = uuid.UUID("11111111-1111-1111-1111-111111111111")
    storage.root.mkdir(parents=True, exist_ok=True)
    link = storage.root / str(planted)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(secret_dir, target_is_directory=True)

    with pytest.raises(InvalidScreenshotRef):
        await storage.load(build_ref(planted, 1))
