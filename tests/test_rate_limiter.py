import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from steam_friend_relationship_map.rate_limiter import AdaptiveRateLimiter
from steam_friend_relationship_map.steam import SteamApiError, SteamClient


@pytest.mark.asyncio
async def test_rate_limiter_starts_immediately_then_spaces_requests() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=300.0)
    loop = MagicMock()
    loop.time.side_effect = [10.0, 10.0, 10.3]

    with (
        patch(
            "steam_friend_relationship_map.rate_limiter.asyncio.get_running_loop",
            return_value=loop,
        ),
        patch("steam_friend_relationship_map.rate_limiter.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await limiter.wait()
        sleep.assert_not_awaited()

        await limiter.wait()
        sleep.assert_awaited_once_with(pytest.approx(0.3))


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_reserve_a_phantom_request_slot() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=300.0)
    loop = MagicMock()
    loop.time.return_value = 10.0
    sleep_started = asyncio.Event()

    async def blocked_sleep(_: float) -> None:
        sleep_started.set()
        await asyncio.Future()

    with (
        patch(
            "steam_friend_relationship_map.rate_limiter.asyncio.get_running_loop",
            return_value=loop,
        ),
        patch(
            "steam_friend_relationship_map.rate_limiter.asyncio.sleep",
            side_effect=blocked_sleep,
        ),
    ):
        await limiter.wait()
        waiting = asyncio.create_task(limiter.wait())
        await sleep_started.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    assert limiter._next_request_at == pytest.approx(10.3)


@pytest.mark.asyncio
async def test_waiting_request_rechecks_backoff_after_waking() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=300.0)
    clock = 10.0
    sleep_calls: list[float] = []
    first_sleep_started = asyncio.Event()
    release_first_sleep = asyncio.Event()

    class Loop:
        @staticmethod
        def time() -> float:
            return clock

    async def advancing_sleep(delay: float) -> None:
        nonlocal clock
        sleep_calls.append(delay)
        if len(sleep_calls) == 1:
            first_sleep_started.set()
            await release_first_sleep.wait()
        clock += delay

    with (
        patch(
            "steam_friend_relationship_map.rate_limiter.asyncio.get_running_loop",
            return_value=Loop(),
        ),
        patch(
            "steam_friend_relationship_map.rate_limiter.asyncio.sleep",
            side_effect=advancing_sleep,
        ),
    ):
        await limiter.wait()
        waiting = asyncio.create_task(limiter.wait())
        await first_sleep_started.wait()
        await limiter.report_backoff(retry_after_ms=1000.0)
        release_first_sleep.set()
        await waiting

    assert sleep_calls == [pytest.approx(0.3), pytest.approx(0.7)]
    assert limiter._next_request_at == pytest.approx(11.45)


@pytest.mark.asyncio
async def test_rate_limiter_can_back_off_from_zero_delay() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=0.0)
    loop = MagicMock()
    loop.time.return_value = 20.0

    with patch(
        "steam_friend_relationship_map.rate_limiter.asyncio.get_running_loop",
        return_value=loop,
    ):
        await limiter.report_backoff(retry_after_ms=1500.0)

    assert limiter.current_delay_ms == 100.0
    assert limiter._next_request_at == pytest.approx(21.5)


@pytest.mark.asyncio
async def test_adaptive_rate_limiter_aimd():
    changes = []
    callback_lock_states = []

    def callback(old_val, new_val, reason):
        changes.append((old_val, new_val, reason))
        callback_lock_states.append(limiter.lock.locked())

    # Base delay 300ms, min 250ms, max 1000ms
    limiter = AdaptiveRateLimiter(
        base_delay_ms=300.0,
        min_delay_ms=250.0,
        max_delay_ms=1000.0,
        on_change_callback=callback,
    )

    assert limiter.current_delay_ms == 300.0

    # 1. Test success (delay decreases by 12.5ms first time)
    await limiter.report_success()
    assert limiter.current_delay_ms == 287.5
    assert len(changes) == 1
    assert changes[-1] == (300.0, 287.5, "success")

    # 2. Test min cap
    await limiter.report_success()  # 275.625
    await limiter.report_success()  # 264.34375
    await limiter.report_success()  # 253.6265625
    await limiter.report_success()  # 250.0 (capped)
    await limiter.report_success()  # should stay 250.0
    assert limiter.current_delay_ms == 250.0

    # 3. Test backoff (multiply by 1.5)
    await limiter.report_backoff()  # 250 * 1.5 = 375
    assert limiter.current_delay_ms == 375.0
    assert changes[-1] == (250.0, 375.0, "backoff")

    # 4. Test max cap
    limiter.current_delay_ms = 800.0
    await limiter.report_backoff()  # 800 * 1.5 = 1200 -> cap at 1000
    assert limiter.current_delay_ms == 1000.0
    assert not any(callback_lock_states)


@pytest.mark.asyncio
async def test_rate_limiter_callback_failure_does_not_break_state_updates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing_callback(_: float, __: float, ___: str) -> None:
        raise RuntimeError("log sink unavailable")

    limiter = AdaptiveRateLimiter(
        base_delay_ms=300.0,
        min_delay_ms=250.0,
        max_delay_ms=1000.0,
        on_change_callback=failing_callback,
    )

    await limiter.report_success()
    assert limiter.current_delay_ms == 287.5

    await limiter.report_backoff()
    assert limiter.current_delay_ms == 431.25
    assert [record.message for record in caplog.records] == [
        "Rate limiter change callback failed",
        "Rate limiter change callback failed",
    ]


@pytest.mark.asyncio
async def test_steam_client_rate_limiter_integration():
    limiter = MagicMock(spec=AdaptiveRateLimiter)
    limiter.wait = AsyncMock()
    limiter.report_success = AsyncMock()
    limiter.report_backoff = AsyncMock()

    client = SteamClient(api_key="mock_key", rate_limiter=limiter)

    # Mock httpx client response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": {"players": []}}

    async_client_mock = AsyncMock()
    async_client_mock.get.return_value = mock_response
    client._client = async_client_mock
    client._owns_client = False

    # Perform request
    await client.get_player_summaries(["12345"])

    # Verify limiter was called
    limiter.wait.assert_called_once()
    limiter.report_success.assert_called_once()
    limiter.report_backoff.assert_not_called()

    # Reset mock and test 429 rate limit
    limiter.wait.reset_mock()
    limiter.report_success.reset_mock()
    limiter.report_backoff.reset_mock()

    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    async_client_mock.get.return_value = mock_response_429

    with pytest.raises(SteamApiError):
        # We set retries to 1 to fail immediately
        await client._get_json("/some/path", {}, retries=1)

    limiter.wait.assert_called_once()
    limiter.report_success.assert_not_called()
    limiter.report_backoff.assert_called_once()
