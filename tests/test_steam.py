from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from steam_friend_relationship_map.rate_limiter import AdaptiveRateLimiter
from steam_friend_relationship_map.steam import SteamApiError, SteamClient, parse_retry_after


def test_parse_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    retry_at = now + timedelta(seconds=12)

    assert parse_retry_after("7.5", now=now) == 7.5
    assert parse_retry_after(retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=now) == 12.0
    assert parse_retry_after("not-a-date", now=now) is None


@pytest.mark.asyncio
async def test_steam_client_honors_retry_after_header() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=0)
    limiter.wait = AsyncMock()
    limiter.report_success = AsyncMock()
    limiter.report_backoff = AsyncMock()
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"response": {"players": []}}),
    ]
    http_client = AsyncMock()
    http_client.get.side_effect = responses
    client = SteamClient("key-1,key-2", client=http_client, rate_limiter=limiter)

    with (
        patch("steam_friend_relationship_map.steam.asyncio.sleep", new=AsyncMock()) as sleep,
        patch("random.uniform", return_value=0.0),
    ):
        result = await client._get_json("/test", {}, retries=2)

    assert result == {"response": {"players": []}}
    assert [
        call.kwargs["params"]["key"] for call in http_client.get.await_args_list
    ] == ["key-1", "key-2"]
    limiter.report_backoff.assert_awaited_once_with(retry_after_ms=7000.0)
    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_steam_client_creates_owned_client_with_explicit_proxy() -> None:
    proxy_url = "socks5h://user:password@127.0.0.1:1080"

    with patch("steam_friend_relationship_map.steam.httpx.AsyncClient") as async_client:
        client = SteamClient("key", proxy_url=proxy_url)
        await client.__aenter__()

    async_client.assert_called_once_with(
        timeout=12,
        proxy=proxy_url,
        trust_env=False,
    )


@pytest.mark.asyncio
async def test_reenter_after_external_client_creates_and_closes_owned_client() -> None:
    external_client = AsyncMock()
    replacement_client = AsyncMock()
    client = SteamClient("key", client=external_client)

    await client.aclose()
    with patch.object(
        client,
        "_create_http_client",
        return_value=replacement_client,
    ):
        async with client:
            assert client._client is replacement_client
            assert client._owns_client is True

    external_client.aclose.assert_not_awaited()
    replacement_client.aclose.assert_awaited_once_with()
    assert client._client is None


@pytest.mark.asyncio
async def test_concurrent_close_only_closes_owned_client_once() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    owned_client = AsyncMock()

    async def delayed_close() -> None:
        close_started.set()
        await release_close.wait()

    owned_client.aclose.side_effect = delayed_close
    client = SteamClient("key")
    client._client = owned_client

    first_close = asyncio.create_task(client.aclose())
    await close_started.wait()
    second_close = asyncio.create_task(client.aclose())
    await asyncio.wait_for(second_close, timeout=1)
    release_close.set()
    await first_close

    owned_client.aclose.assert_awaited_once_with()
    assert client._client is None


@pytest.mark.asyncio
async def test_failed_close_detaches_client_before_propagating_error() -> None:
    failed_client = AsyncMock()
    failed_client.aclose.side_effect = RuntimeError("close failed")
    client = SteamClient("key")
    client._client = failed_client

    with pytest.raises(RuntimeError, match="close failed"):
        await client.aclose()

    assert client._client is None
    assert client._owns_client is True


def test_steam_client_rejects_invalid_proxy_url() -> None:
    with pytest.raises(ValueError, match="proxy URL"):
        SteamClient("key", proxy_url="ftp://127.0.0.1:21")


@pytest.mark.asyncio
async def test_resolve_profiles_url_without_network() -> None:
    client = SteamClient("key")

    steam_id = await client.resolve_steam_id("https://steamcommunity.com/profiles/76561197960435530/")

    assert steam_id == "76561197960435530"


@pytest.mark.asyncio
async def test_resolve_vanity_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/ISteamUser/ResolveVanityURL/v0001/")
        return httpx.Response(200, json={"response": {"success": 1, "steamid": "76561197960435530"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient("key", base_url="https://api.test", client=http_client)
        steam_id = await client.resolve_steam_id("https://steamcommunity.com/id/gabelogannewell")

    assert steam_id == "76561197960435530"


@pytest.mark.asyncio
async def test_invalid_url_raises() -> None:
    client = SteamClient("key")

    with pytest.raises(SteamApiError):
        await client.resolve_steam_id("https://example.com/not-steam")


@pytest.mark.asyncio
async def test_private_friend_list_is_marked() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(401, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient("key-1,key-2", base_url="https://api.test", client=http_client)
        result = await client.get_friend_list("76561197960435530")

    assert result.private is True
    assert result.friend_ids == []
    assert request_count == 1


@pytest.mark.asyncio
async def test_forbidden_friend_list_rotates_keys_and_surfaces_auth_error() -> None:
    queried_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried_keys.append(str(request.url.params.get("key")))
        return httpx.Response(403, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(
            "bad-key-1,bad-key-2",
            base_url="https://api.test",
            client=http_client,
        )
        with pytest.raises(SteamApiError) as error:
            await client.get_friend_list("76561197960435530")
        with pytest.raises(SteamApiError) as repeated_error:
            await client.get_friend_list("76561197960435531")

    assert error.value.status_code == 403
    assert repeated_error.value.status_code == 403
    assert queried_keys == ["bad-key-1", "bad-key-2"]


@pytest.mark.asyncio
async def test_auth_fallback_does_not_mutate_params_or_back_off_good_keys() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=0)
    limiter.wait = AsyncMock()
    limiter.report_success = AsyncMock()
    limiter.report_backoff = AsyncMock()
    queried_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url.params.get("key"))
        queried_keys.append(key)
        if key == "bad-key":
            return httpx.Response(403, json={})
        return httpx.Response(200, json={"response": {"players": []}})

    params = {"steamids": "123", "key": "caller-value"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(
            "bad-key,good-key",
            base_url="https://api.test",
            client=http_client,
            rate_limiter=limiter,
        )
        result = await client._get_json("/test", params, retries=1)
        repeated_result = await client._get_json("/test", params, retries=1)

    assert result == {"response": {"players": []}}
    assert repeated_result == result
    assert queried_keys == ["bad-key", "good-key", "good-key"]
    assert params == {"steamids": "123", "key": "caller-value"}
    assert limiter.wait.await_count == 3
    assert limiter.report_success.await_count == 2
    limiter.report_backoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_key_is_not_reused_after_a_good_key_transient_failure() -> None:
    queried_keys: list[str] = []
    good_key_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal good_key_attempts
        key = str(request.url.params.get("key"))
        queried_keys.append(key)
        if key == "bad-key":
            return httpx.Response(403, json={})
        good_key_attempts += 1
        if good_key_attempts == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"response": {"players": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(
            "bad-key,good-key",
            base_url="https://api.test",
            client=http_client,
        )
        with (
            patch("steam_friend_relationship_map.steam.asyncio.sleep", new=AsyncMock()),
            patch("random.uniform", return_value=0.0),
        ):
            result = await client._get_json("/test", {}, retries=2)

    assert result == {"response": {"players": []}}
    assert queried_keys == ["bad-key", "good-key", "good-key"]
    assert client._disabled_api_keys == {"bad-key"}


@pytest.mark.asyncio
async def test_non_friend_list_unauthorized_response_rotates_api_keys() -> None:
    queried_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url.params.get("key"))
        queried_keys.append(key)
        if key == "bad-key":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"response": {"players": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(
            "bad-key,good-key",
            base_url="https://api.test",
            client=http_client,
        )
        assert await client.get_player_summaries(["123"]) == []
        assert await client.get_player_summaries(["456"]) == []

    assert queried_keys == ["bad-key", "good-key", "good-key"]
    assert client._disabled_api_keys == {"bad-key"}


@pytest.mark.asyncio
async def test_malformed_success_response_only_reports_backoff() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=0)
    limiter.wait = AsyncMock()
    limiter.report_success = AsyncMock()
    limiter.report_backoff = AsyncMock()
    http_client = AsyncMock()
    http_client.get.side_effect = [
        httpx.Response(200, content=b"{"),
        httpx.Response(200, json={"response": {"players": []}}),
    ]
    client = SteamClient("key", client=http_client, rate_limiter=limiter)

    with (
        patch("steam_friend_relationship_map.steam.asyncio.sleep", new=AsyncMock()),
        patch("random.uniform", return_value=0.0),
    ):
        result = await client._get_json("/test", {}, retries=2)

    assert result == {"response": {"players": []}}
    limiter.report_backoff.assert_awaited_once_with()
    limiter.report_success.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_key_disabled_while_rate_limited_is_skipped_before_request() -> None:
    limiter = AdaptiveRateLimiter(base_delay_ms=0)
    limiter.report_success = AsyncMock()
    limiter.report_backoff = AsyncMock()
    queried_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried_keys.append(str(request.url.params.get("key")))
        return httpx.Response(200, json={"response": {"players": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(
            "key-1,key-2",
            base_url="https://api.test",
            client=http_client,
            rate_limiter=limiter,
        )
        wait_count = 0

        async def wait_and_disable_first_key() -> None:
            nonlocal wait_count
            wait_count += 1
            if wait_count == 1:
                client._disabled_api_keys.add("key-1")

        limiter.wait = AsyncMock(side_effect=wait_and_disable_first_key)
        result = await client._get_json("/test", {}, retries=1)

    assert result == {"response": {"players": []}}
    assert queried_keys == ["key-2"]
    assert limiter.wait.await_count == 2


@pytest.mark.asyncio
async def test_steam_client_key_rotation() -> None:
    queried_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.params.get("key")
        queried_keys.append(key)
        return httpx.Response(200, json={"response": {"players": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient(
            "key1, key2; key1\nkey3 key4",
            base_url="https://api.test",
            client=http_client,
        )
        assert client.api_keys == ["key1", "key2", "key3", "key4"]

        await client.get_player_summaries(["123"])
        await client.get_player_summaries(["456"])
        await client.get_player_summaries(["789"])
        await client.get_player_summaries(["012"])
        await client.get_player_summaries(["345"])

    assert queried_keys == ["key1", "key2", "key3", "key4", "key1"]
