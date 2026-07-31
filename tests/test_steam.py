from __future__ import annotations

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
    client = SteamClient("key", client=http_client, rate_limiter=limiter)

    with (
        patch("steam_friend_relationship_map.steam.asyncio.sleep", new=AsyncMock()) as sleep,
        patch("random.uniform", return_value=0.0),
    ):
        result = await client._get_json("/test", {}, retries=2)

    assert result == {"response": {"players": []}}
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
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient("key", base_url="https://api.test", client=http_client)
        result = await client.get_friend_list("76561197960435530")

    assert result.private is True
    assert result.friend_ids == []


@pytest.mark.asyncio
async def test_steam_client_key_rotation() -> None:
    queried_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.params.get("key")
        queried_keys.append(key)
        return httpx.Response(200, json={"response": {"players": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SteamClient("key1, key2; key3\nkey4", base_url="https://api.test", client=http_client)
        assert client.api_keys == ["key1", "key2", "key3", "key4"]

        await client.get_player_summaries(["123"])
        await client.get_player_summaries(["456"])
        await client.get_player_summaries(["789"])
        await client.get_player_summaries(["012"])
        await client.get_player_summaries(["345"])

    assert queried_keys == ["key1", "key2", "key3", "key4", "key1"]
