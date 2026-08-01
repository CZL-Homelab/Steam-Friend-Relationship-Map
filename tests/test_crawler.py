from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from steam_friend_relationship_map.crawler import CrawlControl, CrawlManager
from steam_friend_relationship_map.logs import AppLogBuffer
from steam_friend_relationship_map.models import (
    CrawlCreate,
    CrawlRun,
    CrawlStatus,
    FriendEdge,
    FriendListCacheUpdate,
    SteamUserRecord,
)
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
        self.run_updates: list[dict[str, object]] = []

    def ensure_schema(self) -> None:
        pass

    def start_crawl_run(self, run: CrawlRun, project_id: str = "default") -> None:
        self.runs[run.id] = run

    def update_crawl_run(self, run_id: str, **fields: object) -> None:
        self.run_updates.append(dict(fields))
        run = self.runs[run_id]
        data = run.model_dump()
        data.update(fields)
        self.runs[run_id] = CrawlRun(**data)

    def upsert_users(self, users: list[SteamUserRecord], project_id: str = "default") -> None:
        for user in users:
            self.users[user.steam_id] = user

    def mark_friend_list_status(self, steam_id: str, status: str, **_: object) -> None:
        self.statuses[steam_id] = status

    def upsert_relationships(self, edges: list[FriendEdge], project_id: str = "default") -> None:
        for edge in edges:
            self.edges.add(tuple(sorted((edge.from_id, edge.to_id))))

    def get_cached_friend_list(self, steam_id: str, valid_days: int, project_id: str = "default") -> tuple[str, list[str]] | None:
        return None

    def count_inner_layer_links(self, candidate_ids: list[str], inner_pool: list[str], project_id: str = "default") -> dict[str, int]:
        graph = {
            "root": ["a", "b"],
            "a": ["root", "c"],
            "b": ["root", "c", "private"],
            "c": ["a", "b"],
        }
        res = {}
        for cid in candidate_ids:
            neighbors = graph.get(cid, [])
            links = len([n for n in neighbors if n in inner_pool])
            res[cid] = links
        return res


class TrackingSteam(FakeSteam):
    def __init__(self, friend_graph: dict[str, list[str]]) -> None:
        super().__init__()
        self.friend_graph = friend_graph
        self.active_requests = 0
        self.peak_requests = 0

    async def get_friend_list(self, steam_id: str) -> FriendListResult:
        self.active_requests += 1
        self.peak_requests = max(self.peak_requests, self.active_requests)
        try:
            await asyncio.sleep(0.01)
            return FriendListResult(
                steam_id=steam_id,
                friend_ids=self.friend_graph.get(steam_id, []),
            )
        finally:
            self.active_requests -= 1


class CachedFakeRepo(FakeRepo):
    def __init__(self, cached_lists: dict[str, tuple[str, list[str]]]) -> None:
        super().__init__()
        self.cached_lists = cached_lists

    def get_cached_friend_list(
        self,
        steam_id: str,
        valid_days: int,
        project_id: str = "default",
    ) -> tuple[str, list[str]] | None:
        return self.cached_lists.get(steam_id) if valid_days > 0 else None


class BatchCachedFakeRepo(CachedFakeRepo):
    def __init__(self, cached_lists: dict[str, tuple[str, list[str]]]) -> None:
        super().__init__(cached_lists)
        self.batch_calls = 0

    def get_cached_friend_list(
        self,
        steam_id: str,
        valid_days: int,
        project_id: str = "default",
    ) -> tuple[str, list[str]] | None:
        raise AssertionError("batch cache lookup should not use the singular method")

    def get_cached_friend_lists(
        self,
        steam_ids: list[str],
        valid_days: int,
        project_id: str = "default",
    ) -> dict[str, tuple[str, list[str]]]:
        self.batch_calls += 1
        if valid_days <= 0:
            return {}
        return {
            steam_id: self.cached_lists[steam_id]
            for steam_id in dict.fromkeys(steam_ids)
            if steam_id in self.cached_lists
        }


class BatchStatusFakeRepo(FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.status_batches: list[list[str]] = []

    def mark_friend_list_status(self, steam_id: str, status: str, **_: object) -> None:
        raise AssertionError("batch status persistence should not use the singular method")

    def mark_friend_list_statuses(
        self,
        updates: list[FriendListCacheUpdate],
        project_id: str = "default",
    ) -> None:
        self.status_batches.append([update.steam_id for update in updates])
        for update in updates:
            self.statuses[update.steam_id] = update.status


class HangingSteam(FakeSteam):
    def __init__(self) -> None:
        super().__init__()
        self.friend_request_started = asyncio.Event()

    async def get_friend_list(self, steam_id: str) -> FriendListResult:
        self.friend_request_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingRepo(FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def blocking_write(self) -> None:
        self.started.set()
        self.release.wait(timeout=2)
        self.finished.set()


class PrivateBatchSteam(FakeSteam):
    async def get_friend_list(self, steam_id: str) -> FriendListResult:
        if steam_id == "root":
            return FriendListResult(
                steam_id=steam_id,
                friend_ids=[f"private-{index}" for index in range(6)],
            )
        return FriendListResult(steam_id=steam_id, friend_ids=[], private=True)


class SummaryFailureSteam(FakeSteam):
    async def get_player_summaries(self, steam_ids: list[str]) -> list[SteamUserRecord]:
        raise RuntimeError("request failed api_key=0123456789abcdef0123456789abcdef")


class TerminalWriteFailRepo(FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_attempts = 0

    def update_crawl_run(self, run_id: str, **fields: object) -> None:
        if fields.get("status") in {
            CrawlStatus.completed.value,
            CrawlStatus.cancelled.value,
            CrawlStatus.stopped.value,
            CrawlStatus.failed.value,
        }:
            self.terminal_attempts += 1
            raise RuntimeError("database unavailable")
        super().update_crawl_run(run_id, **fields)


@pytest.mark.asyncio
async def test_crawl_failure_persists_redacted_terminal_snapshot() -> None:
    repo = FakeRepo()
    logs = AppLogBuffer()
    manager = CrawlManager(repo, SummaryFailureSteam(), logs)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0)
    )
    await manager.controls[run.id].task

    finished = repo.runs[run.id]
    assert finished.status == CrawlStatus.failed
    assert "0123456789abcdef" not in finished.message
    assert "[REDACTED]" in finished.message
    assert finished.nodes_discovered == 0
    assert finished.edges_discovered == 0
    assert finished.expanded_count == 0
    assert finished.queue_size == 0
    assert finished.error_count == 1


@pytest.mark.asyncio
async def test_crawl_terminal_write_failure_is_retried_and_contained() -> None:
    repo = TerminalWriteFailRepo()
    logs = AppLogBuffer()
    steam = SummaryFailureSteam()
    manager = CrawlManager(repo, steam, logs)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0)
    )
    await manager.controls[run.id].task

    assert repo.terminal_attempts == 2
    assert repo.runs[run.id].status == CrawlStatus.running
    assert steam.rate_limiter is None
    assert any(
        row.source == "crawl:failed-persist" and "已重试" in row.message
        for row in logs.list()
    )


@pytest.mark.asyncio
async def test_crawl_manager_shutdown_cancels_pending_work_and_rejects_new_runs() -> None:
    steam = HangingSteam()
    repo = FakeRepo()
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]
    run = await manager.create_crawl(
        CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0)
    )
    await asyncio.wait_for(steam.friend_request_started.wait(), timeout=1)

    await manager.shutdown(timeout_seconds=0)

    assert manager.controls[run.id].task is not None
    assert manager.controls[run.id].task.done()
    assert manager.has_active_crawl() is False
    assert repo.runs[run.id].status == CrawlStatus.stopped
    assert "应用关闭" in repo.runs[run.id].message
    with pytest.raises(RuntimeError, match="应用正在关闭"):
        await manager.create_crawl(
            CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0)
        )


@pytest.mark.asyncio
async def test_crawler_repository_work_does_not_block_event_loop() -> None:
    repo = BlockingRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    try:
        operation = asyncio.create_task(manager._call_repo("blocking_write"))
        while not repo.started.is_set():
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.01)
        responsive_after = loop.time() - started_at
        repo.release.set()
        await operation
    finally:
        repo.release.set()

    assert responsive_after < 0.5
    assert repo.finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_crawler_waits_for_started_repository_work() -> None:
    repo = BlockingRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]
    operation = asyncio.create_task(manager._call_repo("blocking_write"))
    while not repo.started.is_set():
        await asyncio.sleep(0.005)

    operation.cancel()
    await asyncio.sleep(0.01)
    assert operation.done() is False

    repo.release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert repo.finished.is_set()


@pytest.mark.asyncio
async def test_pause_and_resume_roll_back_memory_state_when_persistence_fails() -> None:
    repo = FakeRepo()

    def fail_update(_run_id: str, **_fields: object) -> None:
        raise RuntimeError("database unavailable")

    repo.update_crawl_run = fail_update  # type: ignore[method-assign]
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]
    control = CrawlControl()
    manager.controls["run"] = control

    with pytest.raises(RuntimeError, match="database unavailable"):
        await manager.pause("run")
    assert control.pause is False

    control.pause = True
    with pytest.raises(RuntimeError, match="database unavailable"):
        await manager.resume("run")
    assert control.pause is True


def test_crawler_repository_calls_use_async_wrapper() -> None:
    source = Path("src/steam_friend_relationship_map/crawler.py").read_text(
        encoding="utf-8"
    )

    assert "self.repo." not in source


@pytest.mark.asyncio
async def test_crawl_uses_cached_friend_lists_without_api_requests() -> None:
    steam = TrackingSteam({})
    repo = CachedFakeRepo({"root": ("public", ["a"])})
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0)
    )
    await manager.controls[run.id].task

    assert repo.runs[run.id].status == CrawlStatus.completed
    assert set(repo.users) == {"root", "a"}
    assert steam.peak_requests == 0


@pytest.mark.asyncio
async def test_crawl_reads_each_friend_cache_batch_with_one_repository_call() -> None:
    steam = TrackingSteam({"missing": ["friend"]})
    repo = BatchCachedFakeRepo(
        {
            "cached": ("public", ["known"]),
            "private": ("private", []),
        }
    )
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    lookups = await manager._load_friend_list_batch(
        ["cached", "private", "missing"], cache_valid_days=14
    )

    assert repo.batch_calls == 1
    assert [(lookup.steam_id, lookup.source, lookup.status) for lookup in lookups] == [
        ("cached", "cache", "public"),
        ("private", "cache", "private"),
        ("missing", "api", "public"),
    ]
    assert lookups[0].friend_ids == ("known",)
    assert lookups[2].friend_ids == ("friend",)
    assert steam.peak_requests == 1


@pytest.mark.asyncio
async def test_crawl_persists_each_api_friend_batch_with_one_repository_call() -> None:
    repo = BatchStatusFakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=2,
            max_nodes=10,
            delay_ms=0,
            request_concurrency=2,
        )
    )
    await manager.controls[run.id].task

    assert repo.runs[run.id].status == CrawlStatus.completed
    assert repo.status_batches == [["root"], ["a", "b"]]
    assert repo.statuses == {"root": "public", "a": "public", "b": "public"}


@pytest.mark.asyncio
async def test_crawl_bounds_concurrent_layer_expansion() -> None:
    friend_ids = [f"f{index}" for index in range(6)]
    steam = TrackingSteam({"root": friend_ids})
    repo = FakeRepo()
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=2,
            max_nodes=20,
            delay_ms=0,
            request_concurrency=3,
        )
    )
    await manager.controls[run.id].task

    assert repo.runs[run.id].status == CrawlStatus.completed
    assert steam.peak_requests == 3
    assert set(repo.users) == {"root", *friend_ids}


@pytest.mark.asyncio
async def test_crawl_persists_expansion_progress_once_per_request_batch() -> None:
    friend_ids = [f"friend-{index:02d}" for index in range(12)]
    steam = TrackingSteam({"root": friend_ids, **{steam_id: [] for steam_id in friend_ids}})
    repo = FakeRepo()
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=2,
            max_nodes=20,
            delay_ms=0,
            request_concurrency=4,
        )
    )
    await manager.controls[run.id].task

    progress_updates = [
        update for update in repo.run_updates if "current_steam_id" in update
    ]
    assert len(progress_updates) == 4
    assert progress_updates[-1]["queue_size"] == 0
    assert progress_updates[-1]["expanded_count"] == 13
    assert repo.runs[run.id].status == CrawlStatus.completed


@pytest.mark.asyncio
async def test_crawl_coalesces_private_counts_per_request_batch() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, PrivateBatchSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=2,
            max_nodes=10,
            delay_ms=0,
            request_concurrency=6,
        )
    )
    await manager.controls[run.id].task

    private_only_updates = [
        update for update in repo.run_updates if set(update) == {"private_count"}
    ]
    assert private_only_updates == [{"private_count": 6}]
    assert repo.runs[run.id].private_count == 6
    assert repo.runs[run.id].status == CrawlStatus.completed


@pytest.mark.asyncio
async def test_crawl_bounds_concurrent_friend_count_filter_requests() -> None:
    friend_ids = [f"f{index}" for index in range(6)]
    steam = TrackingSteam({"root": friend_ids})
    repo = FakeRepo()
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=1,
            max_nodes=20,
            delay_ms=0,
            request_concurrency=2,
            friend_count_min=0,
        )
    )
    await manager.controls[run.id].task

    assert repo.runs[run.id].status == CrawlStatus.completed
    assert steam.peak_requests == 2
    assert all(repo.users[steam_id].friend_count == 0 for steam_id in friend_ids)


@pytest.mark.asyncio
async def test_crawl_persists_new_edges_between_same_layer_users() -> None:
    steam = TrackingSteam(
        {
            "root": ["a", "b"],
            "a": ["root", "b"],
            "b": ["root", "a"],
        }
    )
    repo = FakeRepo()
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=2,
            max_nodes=10,
            delay_ms=0,
            request_concurrency=2,
        )
    )
    await manager.controls[run.id].task

    assert repo.runs[run.id].status == CrawlStatus.completed
    assert repo.runs[run.id].edges_discovered == 3
    assert ("a", "b") in repo.edges


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

    assert set(repo.users) == {"root", "a", "b"}
    assert repo.users["b"].friend_count == 3
    assert repo.runs[run.id].friend_count_filtered_count == 1


@pytest.mark.asyncio
async def test_crawl_filters_by_prior_pool_links() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0, prior_pool_min_links=2))
    await manager.controls[run.id].task

    assert set(repo.users) == {"root", "a", "b"}
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


@pytest.mark.asyncio
async def test_crawl_concurrency_lock() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    run1 = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0))
    with pytest.raises(RuntimeError, match="已有活跃的抓取任务在运行中"):
        await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=10, delay_ms=0))

    await manager.controls[run1.id].task


@pytest.mark.asyncio
async def test_crawl_memory_leak_gc() -> None:
    repo = FakeRepo()
    manager = CrawlManager(repo, FakeSteam())  # type: ignore[arg-type]

    runs = []
    for _ in range(15):
        run = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=2, delay_ms=0))
        await manager.controls[run.id].task
        runs.append(run.id)

    run_16 = await manager.create_crawl(CrawlCreate(root_url="root", max_depth=1, max_nodes=2, delay_ms=0))
    await manager.controls[run_16.id].task

    for old_run_id in runs[:5]:
        assert old_run_id not in manager.controls
        assert old_run_id not in manager.events
        assert old_run_id not in manager.event_seq

    for recent_run_id in runs[5:]:
        assert recent_run_id in manager.controls
        assert recent_run_id in manager.events


class MockAuthBreakerSteam:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_steam_id(self, value: str) -> str:
        return value

    async def get_player_summaries(self, steam_ids: list[str]) -> list[SteamUserRecord]:
        return [placeholder_user(steam_id, 0) for steam_id in steam_ids]

    async def get_friend_list(self, steam_id: str) -> FriendListResult:
        self.calls += 1
        if steam_id == "root":
            return FriendListResult(steam_id="root", friend_ids=[f"f{index}" for index in range(1, 11)])
        from steam_friend_relationship_map.steam import SteamApiError
        raise SteamApiError("Unauthorized", status_code=401)


@pytest.mark.asyncio
async def test_crawl_auth_error_circuit_breaker() -> None:
    repo = FakeRepo()
    steam = MockAuthBreakerSteam()
    manager = CrawlManager(repo, steam)  # type: ignore[arg-type]

    run = await manager.create_crawl(
        CrawlCreate(
            root_url="root",
            max_depth=2,
            max_nodes=100,
            delay_ms=0,
            request_concurrency=3,
        )
    )
    await manager.controls[run.id].task

    finished = repo.runs[run.id]
    assert finished.status == CrawlStatus.failed
    assert "认证失败连续超过 5 次" in finished.message
    assert finished.error_count >= 5
    assert finished.nodes_discovered == 11
    assert finished.edges_discovered == 10
    assert finished.expanded_count == 7
    assert finished.queue_size == 0
    assert steam.calls == 7
    error_only_updates = [
        update for update in repo.run_updates if set(update) == {"error_count"}
    ]
    assert error_only_updates == [{"error_count": 3}]
