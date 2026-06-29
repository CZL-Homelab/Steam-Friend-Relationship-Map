from __future__ import annotations

import httpx
import pytest

from steam_friend_relationship_map.steam import SteamApiError, SteamClient


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
