from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .models import SteamUserRecord


STEAM_ID_RE = re.compile(r"^\d{17}$")


class SteamApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FriendListResult:
    steam_id: str
    friend_ids: list[str]
    private: bool = False


class SteamClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.steampowered.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "SteamClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=12)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def resolve_steam_id(self, value: str) -> str:
        # 支持直接输入 64 位 SteamID，也支持 Steam 主页 URL。
        raw = value.strip()
        if STEAM_ID_RE.match(raw):
            return raw

        parsed = urlparse(raw if "://" in raw else f"https://steamcommunity.com/id/{raw}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "profiles" and STEAM_ID_RE.match(parts[1]):
            return parts[1]
        if len(parts) >= 2 and parts[0].lower() == "id":
            return await self.resolve_vanity_url(parts[1])

        raise SteamApiError("请输入 Steam 64 位 ID、/profiles/<id> 或 /id/<vanity> 主页 URL")

    async def resolve_vanity_url(self, vanity: str) -> str:
        data = await self._get_json(
            "/ISteamUser/ResolveVanityURL/v0001/",
            {"key": self.api_key, "vanityurl": vanity},
        )
        response = data.get("response", {})
        if response.get("success") != 1 or not response.get("steamid"):
            raise SteamApiError(f"无法解析 Steam vanity URL: {vanity}")
        return str(response["steamid"])

    async def get_player_summaries(self, steam_ids: list[str]) -> list[SteamUserRecord]:
        if not steam_ids:
            return []
        # Steam GetPlayerSummaries 支持批量 steamids，这里按 100 个一组降低请求次数。
        chunks: list[list[str]] = [steam_ids[index : index + 100] for index in range(0, len(steam_ids), 100)]
        records: list[SteamUserRecord] = []
        for chunk in chunks:
            data = await self._get_json(
                "/ISteamUser/GetPlayerSummaries/v0002/",
                {"key": self.api_key, "steamids": ",".join(chunk)},
            )
            players = data.get("response", {}).get("players", [])
            for player in players:
                steam_id = str(player.get("steamid", ""))
                if not steam_id:
                    continue
                records.append(
                    SteamUserRecord(
                        steam_id=steam_id,
                        persona_name=player.get("personaname") or "Unknown",
                        profile_url=player.get("profileurl") or f"https://steamcommunity.com/profiles/{steam_id}",
                        avatar=player.get("avatar") or "",
                        avatar_medium=player.get("avatarmedium") or "",
                        avatar_full=player.get("avatarfull") or "",
                        visibility_state=player.get("communityvisibilitystate"),
                        profile_state=player.get("profilestate"),
                    )
                )
        return records

    async def get_friend_list(self, steam_id: str) -> FriendListResult:
        try:
            data = await self._get_json(
                "/ISteamUser/GetFriendList/v0001/",
                {"key": self.api_key, "steamid": steam_id, "relationship": "friend"},
            )
        except SteamApiError as exc:
            # 私密或不可访问的好友列表不视为致命错误，交给抓取器标记分支状态。
            if exc.status_code in {401, 403, 404}:
                return FriendListResult(steam_id=steam_id, friend_ids=[], private=True)
            raise
        friends = data.get("friendslist", {}).get("friends", [])
        return FriendListResult(steam_id=steam_id, friend_ids=[str(item["steamid"]) for item in friends if item.get("steamid")])

    async def _get_json(self, path: str, params: dict[str, str], retries: int = 3) -> dict:
        # 安全注意事项：Steam Web API 要求 api_key 作为 URL 查询参数传递（GET ?key=...）。
        # 虽然通过 HTTPS 加密传输，但 key 会出现在服务器访问日志和可能的中间代理日志中。
        # 应用层日志已通过 AppLogBuffer.redact() 脱敏处理。
        if not self.api_key:
            raise SteamApiError("缺少 STEAM_API_KEY")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=12)
            self._owns_client = True

        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = await self._client.get(url, params=params)
                # 429 和 5xx 通常是临时问题，做轻量退避后重试。
                if response.status_code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise SteamApiError(f"Steam API 请求失败: HTTP {response.status_code}", response.status_code)
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < retries - 1:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
        raise SteamApiError(f"Steam API 请求失败: {last_error}")


def placeholder_user(steam_id: str, depth: int) -> SteamUserRecord:
    return SteamUserRecord(
        steam_id=steam_id,
        persona_name=f"Steam {steam_id[-6:]}",
        profile_url=f"https://steamcommunity.com/profiles/{steam_id}",
        depth_min=depth,
    )
