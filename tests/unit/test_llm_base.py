"""B12: the provider-agnostic interface and its retry policy."""

import random

import pytest

from app.agent.llm.base import (
    DEFAULT_MAX_ATTEMPTS,
    FallbackLLM,
    LLMBadOutput,
    LLMClient,
    LLMMessage,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
    LLMUsage,
    MessageRole,
    RetryingLLM,
    is_retryable,
)
from app.agent.llm.mock import MockLLM


class RecordingSleep:
    """Stands in for asyncio.sleep so the suite never waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def retrying(inner: LLMClient, sleep: RecordingSleep, **kwargs: object) -> RetryingLLM:
    return RetryingLLM(
        inner,
        sleep=sleep,
        rng=random.Random(1234),
        **kwargs,  # type: ignore[arg-type]
    )


async def call(client: LLMClient) -> LLMResponse:
    return await client.generate_json(
        "system", [LLMMessage(role=MessageRole.USER, text="goal")], {}
    )


# ------------------------------------------------------------------ types


def test_usage_totals() -> None:
    assert LLMUsage(input_tokens=100, output_tokens=25).total_tokens == 125


def test_a_message_knows_it_carries_images() -> None:
    assert LLMMessage(role=MessageRole.USER, text="x", images=[b"jpeg"]).has_images
    assert not LLMMessage(role=MessageRole.USER, text="x").has_images


def test_the_mock_satisfies_the_protocol() -> None:
    """Everything above this layer depends on the protocol, never a vendor SDK."""
    assert isinstance(MockLLM(), LLMClient)


# ------------------------------------------------------------------ retry


@pytest.mark.parametrize("error", [LLMTimeout, LLMRateLimited, LLMUnavailable])
def test_transport_failures_are_retryable(error: type[Exception]) -> None:
    assert is_retryable(error("x"))


def test_bad_output_is_never_retryable() -> None:
    """Re-sending an identical prompt gets an identical bad answer, for money."""
    assert not is_retryable(LLMBadOutput("not json"))


async def test_a_transient_failure_is_retried() -> None:
    sleep = RecordingSleep()
    client = retrying(MockLLM(fail_times=2, failure=LLMUnavailable), sleep)

    response = await call(client)

    assert response.provider == "mock"
    assert len(sleep.delays) == 2


async def test_retries_stop_at_the_limit() -> None:
    sleep = RecordingSleep()
    inner = MockLLM(fail_times=99, failure=LLMTimeout)
    client = retrying(inner, sleep)

    with pytest.raises(LLMTimeout):
        await call(client)

    assert inner.call_count == DEFAULT_MAX_ATTEMPTS
    assert len(sleep.delays) == DEFAULT_MAX_ATTEMPTS - 1


async def test_bad_output_is_not_retried() -> None:
    sleep = RecordingSleep()
    inner = MockLLM(malformed_times=99)
    client = retrying(inner, sleep)

    with pytest.raises(LLMBadOutput):
        await call(client)

    assert inner.call_count == 1
    assert sleep.delays == []


async def test_an_unexpected_error_is_not_retried() -> None:
    """Only failures where nothing about the request was wrong get another go."""
    sleep = RecordingSleep()
    inner = MockLLM(fail_times=99, failure=ValueError)
    client = retrying(inner, sleep)

    with pytest.raises(ValueError, match="mock failure"):
        await call(client)

    assert inner.call_count == 1


async def test_backoff_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = RecordingSleep()
    # Full jitter picks from [0, ceiling); pin it to the ceiling to see growth.
    client = RetryingLLM(
        MockLLM(fail_times=99, failure=LLMUnavailable),
        sleep=sleep,
        max_attempts=4,
        base_delay_s=1.0,
    )
    monkeypatch.setattr(client._rng, "uniform", lambda _low, high: high)

    with pytest.raises(LLMUnavailable):
        await call(client)

    assert sleep.delays == [1.0, 2.0, 4.0]


async def test_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = RecordingSleep()
    client = RetryingLLM(
        MockLLM(fail_times=99, failure=LLMUnavailable),
        sleep=sleep,
        max_attempts=6,
        base_delay_s=1.0,
        max_delay_s=3.0,
    )
    monkeypatch.setattr(client._rng, "uniform", lambda _low, high: high)

    with pytest.raises(LLMUnavailable):
        await call(client)

    assert max(sleep.delays) == 3.0


async def test_jitter_spreads_retries() -> None:
    """Without it, tasks that hit one rate limit retry in lockstep and hit it again."""
    delays: set[float] = set()
    for seed in range(8):
        sleep = RecordingSleep()
        client = RetryingLLM(
            MockLLM(fail_times=1, failure=LLMUnavailable),
            sleep=sleep,
            rng=random.Random(seed),
        )
        await call(client)
        delays.add(sleep.delays[0])

    assert len(delays) > 1


async def test_a_server_supplied_retry_after_is_honoured() -> None:
    """Guessing a backoff when the server has already told us wastes the very
    quota we are short of."""
    sleep = RecordingSleep()
    inner = MockLLM()
    original = inner.generate_json
    first = {"pending": True}

    async def rate_limited_once(*args: object, **kwargs: object) -> LLMResponse:
        if first["pending"]:
            first["pending"] = False
            raise LLMRateLimited("slow down", retry_after_s=2.5)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    inner.generate_json = rate_limited_once  # type: ignore[method-assign]

    await call(retrying(inner, sleep))

    assert sleep.delays == [2.5]


def test_max_attempts_must_be_sane() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryingLLM(MockLLM(), max_attempts=0)


def test_the_wrapper_is_transparent() -> None:
    inner = MockLLM(model_name="mock-7")
    client = RetryingLLM(inner)

    assert client.provider == "mock"
    assert client.model_name == "mock-7"
    assert client.inner is inner


# ------------------------------------------------------------------ fallback


async def test_fallback_takes_over_when_the_primary_is_down() -> None:
    primary = MockLLM(fail_times=99, failure=LLMUnavailable, model_name="primary")
    secondary = MockLLM(model_name="secondary")
    client = FallbackLLM(
        retrying(primary, RecordingSleep()),
        secondary,
    )

    response = await call(client)

    assert response.model_name == "secondary"


async def test_fallback_does_not_mask_a_bad_answer() -> None:
    """A healthy provider answering badly is a prompt problem, not an outage."""
    primary = MockLLM(malformed_times=99, model_name="primary")
    secondary = MockLLM(model_name="secondary")
    client = FallbackLLM(primary, secondary)

    with pytest.raises(LLMBadOutput):
        await call(client)

    assert secondary.call_count == 0


# ------------------------------------------------------------------ pacing


async def test_the_pacer_spaces_calls_by_the_interval() -> None:
    from app.agent.llm.base import RequestPacer

    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = RequestPacer(12.0, clock=lambda: now[0], sleep=sleep)

    for _ in range(3):
        await pacer.wait_turn()

    # The first call goes straight through; each later one waits for its turn.
    assert slept == [12.0, 12.0]


async def test_a_call_after_a_long_gap_does_not_wait() -> None:
    from app.agent.llm.base import RequestPacer

    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = RequestPacer(12.0, clock=lambda: now[0], sleep=sleep)
    await pacer.wait_turn()
    now[0] = 100.0
    await pacer.wait_turn()

    assert slept == []
