import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

from steam_friend_relationship_map.rate_limiter import AdaptiveRateLimiter
from steam_friend_relationship_map.steam import SteamClient, SteamApiError


@pytest.mark.asyncio
async def test_adaptive_rate_limiter_aimd():
    changes = []
    def callback(old_val, new_val, reason):
        changes.append((old_val, new_val, reason))

    # Base delay 300ms, min 250ms, max 1000ms
    limiter = AdaptiveRateLimiter(
        base_delay_ms=300.0,
        min_delay_ms=250.0,
        max_delay_ms=1000.0,
        on_change_callback=callback
    )

    assert limiter.current_delay_ms == 300.0

    # 1. Test success (delay decreases by 10ms)
    await limiter.report_success()
    assert limiter.current_delay_ms == 290.0
    assert len(changes) == 1
    assert changes[-1] == (300.0, 290.0, "success")

    # 2. Test min cap
    await limiter.report_success()  # 280
    await limiter.report_success()  # 270
    await limiter.report_success()  # 260
    await limiter.report_success()  # 250
    await limiter.report_success()  # should stay 250
    assert limiter.current_delay_ms == 250.0

    # 3. Test backoff (multiply by 1.5)
    await limiter.report_backoff()  # 250 * 1.5 = 375
    assert limiter.current_delay_ms == 375.0
    assert changes[-1] == (250.0, 375.0, "backoff")

    # 4. Test max cap
    limiter.current_delay_ms = 800.0
    await limiter.report_backoff()  # 800 * 1.5 = 1200 -> cap at 1000
    assert limiter.current_delay_ms == 1000.0


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
