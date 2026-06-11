from __future__ import annotations

import pytest

from steam_friend_relationship_map.crawler import CrawlManager
from steam_friend_relationship_map.models import CrawlCreate, CrawlRun, CrawlStatus, FriendEdge, SteamUserRecord
from steam_friend_relationship_map.steam import FriendListResult, placeholder_user


class FakeSteam:
    def __init__(self) -> None:
        self.friend_graph = {
            "root": ["a", "b"],
            "a": ["root", "c"],
            "b": ["root", "c", "private"],
            "c": ["a", "b"],
        }

    async def resolve_steam_id(self, value: str) -> str:
        return value

    async def get_player_summaries(self, steam_ids: list[str]) -> list[SteamUserRecord]:
        return [placeholder_user(steam_id, 0) for steam_id in steam_ids]

    async def get_friend_list(self, steam_id: str) -> FriendListResult:
        if steam_id == "private":
            return FriendListResult(steam_id=steam_id, friend_ids=[], private=True)
        return FriendListResult(steam_id=steam_id, friend_ids=self.friend_graph.get(steam_id, []))


class FakeRepo:
    def __init__(self) -> None:
        self.runs: dict[str, CrawlRun] = {}
        self.users: dict[str, SteamUserRecord] = {}
        self.edges: set[tuple[str, str]] = set()
        self.statuses: dict[str, str] = {}

    def ensure_schema(self) -> None:
        pass

    def start_crawl_run(self, run: CrawlRun) -> None:
        self.runs[run.id] = run

    def update_crawl_run(self, run_id: str, **fields: object) -> None:
        run = self.runs[run_id]
        data = run.model_dump()
        data.update(fields)
        self.runs[run_id] = CrawlRun(**data)

    def upsert_users(self, users: list[SteamUserRecord]) -> None:
        for user in users:
            self.users[user.steam_id] = user

    def mark_friend_list_status(self, steam_id: str, status: str, **_: object) -> None:
        self.statuses[steam_id] = status

    def upsert_relationships(self, edges: list[FriendEdge]) -> None:
        for edge in edges:
            self.edges.add(tuple(sorted((edge.from_id, edge.to_id))))


@pytest.mark.asyncio
async def test_crawl_respects_depth_and_records_private_nodes() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=3, max_nodes=10, delay_ms=0))
    await manager.controls[run.id].task

    finished = repo.runs[run.id]
    assert finished.status == CrawlStatus.completed
    assert set(repo.users) == {"root", "a", "b", "c", "private"}
    assert repo.statuses["private"] == "private"
    assert ("a", "c") in repo.edges
    assert ("b", "c") in repo.edges


@pytest.mark.asyncio
async def test_crawl_respects_max_nodes() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=4, max_nodes=3, delay_ms=0))
    await manager.controls[run.id].task

    assert set(repo.users) == {"root", "a", "b"}
    assert repo.runs[run.id].nodes_discovered == 3


@pytest.mark.asyncio
async def test_crawl_filters_by_friend_count() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0, friend_count_min=3))
    await manager.controls[run.id].task

    assert set(repo.users) == {"root", "b"}
    assert repo.users["b"].friend_count == 3
    assert repo.runs[run.id].friend_count_filtered_count == 1


@pytest.mark.asyncio
async def test_crawl_filters_by_prior_pool_links() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0, prior_pool_min_links=2))
    await manager.controls[run.id].task

    assert set(repo.users) == {"root"}
    assert repo.runs[run.id].prior_pool_filtered_count == 2


@pytest.mark.asyncio
async def test_crawl_events_can_be_read_after_sequence() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0))
    await manager.controls[run.id].task
    events = manager.get_events(run.id, after=1)

    assert events
    assert all(event.seq > 1 for event in events)
    assert "secret" not in " ".join(event.message.lower() for event in events)
