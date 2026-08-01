from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from .models import SteamUserRecord
from .proxy import normalize_proxy_url
from .rate_limiter import AdaptiveRateLimiter


STEAM_ID_RE = re.compile(r"^\d{17}$")


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (retry_at - current).total_seconds())


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
        proxy_url: str = "",
        client: httpx.AsyncClient | None = None,
        rate_limiter: AdaptiveRateLimiter | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxy_url = normalize_proxy_url(proxy_url)
        self._client = client
        self._owns_client = client is None
        self.rate_limiter = rate_limiter
        # Parse potential multiple keys separated by whitespace, commas, or semicolons
        keys = list(
            dict.fromkeys(
                key.strip()
                for key in re.split(r"[\s,;]+", api_key)
                if key.strip()
            )
        )
        self.api_keys = keys if keys else [""]
        self._key_index = 0
        self._disabled_api_keys: set[str] = set()

    def _get_api_key_sequence(self) -> list[str]:
        """Reserve one round-robin start key and return this request's fallback order."""
        available_keys = [
            key for key in self.api_keys if key and key not in self._disabled_api_keys
        ]
        if not available_keys:
            return []
        start = self._key_index % len(available_keys)
        self._key_index = (start + 1) % len(available_keys)
        return available_keys[start:] + available_keys[:start]

    async def __aenter__(self) -> "SteamClient":
        if self._client is None:
            self._client = self._create_http_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    def _create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=12,
            proxy=self.proxy_url or None,
            trust_env=not bool(self.proxy_url),
        )

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
            if exc.status_code in {401, 404}:
                return FriendListResult(steam_id=steam_id, friend_ids=[], private=True)
            raise
        friends = data.get("friendslist", {}).get("friends", [])
        return FriendListResult(steam_id=steam_id, friend_ids=[str(item["steamid"]) for item in friends if item.get("steamid")])

    async def _get_json(self, path: str, params: dict[str, str], retries: int = 3) -> dict:
        # 安全注意事项：Steam Web API 要求 api_key 作为 URL 查询参数传递（GET ?key=...）。
        # 虽然通过 HTTPS 加密传输，但 key 会出现在服务器访问日志和可能的中间代理日志中。
        # 应用层日志已通过 AppLogBuffer.redact() 脱敏处理。
        request_keys = self._get_api_key_sequence()
        if self.api_keys == [""]:
            raise SteamApiError("缺少 STEAM_API_KEY")
        if not request_keys:
            raise SteamApiError("所有已配置的 Steam API Key 均已被拒绝", 403)
        if self._client is None:
            self._client = self._create_http_client()
            self._owns_client = True

        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        transient_limit = max(1, int(retries))
        transient_failures = 0
        auth_failures = 0
        max_attempts = transient_limit + len(request_keys) - 1
        request_params = {key: value for key, value in params.items() if key != "key"}
        for attempt in range(max_attempts):
            current_key = request_keys[attempt % len(request_keys)]
            try:
                if self.rate_limiter:
                    await self.rate_limiter.wait()
                response = await self._client.get(
                    url,
                    params={**request_params, "key": current_key},
                )
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if response.status_code == 403:
                    self._disabled_api_keys.add(current_key)
                    auth_failures += 1
                    if auth_failures < len(request_keys):
                        continue
                    raise SteamApiError(
                        f"Steam API 请求失败: HTTP {response.status_code}",
                        response.status_code,
                    )
                if response.status_code in {429, 500, 502, 503, 504}:
                    if self.rate_limiter:
                        await self.rate_limiter.report_backoff(
                            retry_after_ms=(
                                retry_after * 1000.0
                                if retry_after is not None
                                else None
                            )
                        )
                    transient_failures += 1
                    if transient_failures < transient_limit:
                        delay = max(
                            1.2 * (2 ** (transient_failures - 1)),
                            retry_after or 0.0,
                        )
                        await asyncio.sleep(delay + random.uniform(0.1, 0.5 * delay))
                        continue
                    raise SteamApiError(
                        f"Steam API 请求失败: HTTP {response.status_code}",
                        response.status_code,
                    )
                if response.status_code >= 400:
                    raise SteamApiError(
                        f"Steam API 请求失败: HTTP {response.status_code}",
                        response.status_code,
                    )
                if self.rate_limiter:
                    await self.rate_limiter.report_success()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if self.rate_limiter:
                    await self.rate_limiter.report_backoff()
                transient_failures += 1
                if transient_failures < transient_limit:
                    delay = 1.2 * (2 ** (transient_failures - 1))
                    await asyncio.sleep(delay + random.uniform(0.1, 0.5 * delay))
                    continue
                break
        raise SteamApiError(f"Steam API 请求失败: {last_error}")


def placeholder_user(steam_id: str, depth: int) -> SteamUserRecord:
    return SteamUserRecord(
        steam_id=steam_id,
        persona_name=f"Steam {steam_id[-6:]}",
        profile_url=f"https://steamcommunity.com/profiles/{steam_id}",
        depth_min=depth,
    )
