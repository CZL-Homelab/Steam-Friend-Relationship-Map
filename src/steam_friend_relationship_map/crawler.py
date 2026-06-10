from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass

from .models import CrawlCreate, CrawlRun, CrawlStatus, FriendEdge, SteamUserRecord, utc_now_iso
from .neo4j_repo import Neo4jRepository
from .steam import SteamApiError, SteamClient, placeholder_user


@dataclass
class CrawlControl:
    cancel: bool = False
    task: asyncio.Task | None = None


class CrawlManager:
    def __init__(self, repo: Neo4jRepository, steam: SteamClient) -> None:
        self.repo = repo
        self.steam = steam
        self.controls: dict[str, CrawlControl] = {}

    async def create_crawl(self, payload: CrawlCreate) -> CrawlRun:
        root_steam_id = await self.steam.resolve_steam_id(payload.root_url)
        run = CrawlRun(
            id=str(uuid.uuid4()),
            root_steam_id=root_steam_id,
            max_depth=payload.max_depth,
            max_nodes=payload.max_nodes,
            status=CrawlStatus.pending,
            started_at=utc_now_iso(),
            nodes_discovered=0,
            edges_discovered=0,
        )
        self.repo.ensure_schema()
        self.repo.start_crawl_run(run)
        control = CrawlControl()
        self.controls[run.id] = control
        control.task = asyncio.create_task(self._run_crawl(run, payload.delay_ms, control))
        return run

    def cancel(self, run_id: str) -> bool:
        control = self.controls.get(run_id)
        if control is None:
            return False
        control.cancel = True
        return True

    async def _run_crawl(self, run: CrawlRun, delay_ms: int, control: CrawlControl) -> None:
        # discovered 保存每个节点首次发现的最短层级，避免环路导致重复入队。
        discovered: dict[str, int] = {run.root_steam_id: 0}
        expanded: set[str] = set()
        edges_seen: set[tuple[str, str]] = set()
        queue: deque[tuple[str, int]] = deque([(run.root_steam_id, 0)])
        private_count = 0
        error_count = 0
        try:
            self.repo.update_crawl_run(run.id, status=CrawlStatus.running.value, message="正在抓取 Root 用户资料")
            root_records = await self.steam.get_player_summaries([run.root_steam_id])
            root = root_records[0] if root_records else placeholder_user(run.root_steam_id, 0)
            root.depth_min = 0
            self.repo.upsert_users([root])
            nodes_discovered = 1
            edges_discovered = 0

            while queue:
                if control.cancel:
                    self.repo.update_crawl_run(
                        run.id,
                        status=CrawlStatus.cancelled.value,
                        finished_at=utc_now_iso(),
                        nodes_discovered=nodes_discovered,
                        edges_discovered=edges_discovered,
                        private_count=private_count,
                        error_count=error_count,
                        message="用户已取消",
                    )
                    return

                current_id, depth = queue.popleft()
                # 只展开 max_depth 以内的节点；边界层节点只作为结果保留，不继续向外扩张。
                if current_id in expanded or depth >= run.max_depth:
                    continue
                expanded.add(current_id)
                self.repo.update_crawl_run(run.id, message=f"正在展开深度 {depth}: {current_id}")

                try:
                    friends = await self.steam.get_friend_list(current_id)
                except SteamApiError:
                    error_count += 1
                    self.repo.update_crawl_run(run.id, error_count=error_count)
                    continue

                if friends.private:
                    private_count += 1
                    self.repo.mark_friend_list_status(current_id, "private")
                    self.repo.update_crawl_run(run.id, private_count=private_count)
                    continue

                self.repo.mark_friend_list_status(current_id, "public")
                new_ids: list[str] = []
                new_edges: list[FriendEdge] = []
                next_depth = depth + 1
                for friend_id in friends.friend_ids:
                    if friend_id not in discovered:
                        # max_nodes 是硬上限，达到后不再创建外围占位节点。
                        if len(discovered) >= run.max_nodes:
                            continue
                        discovered[friend_id] = next_depth
                        new_ids.append(friend_id)
                        if next_depth < run.max_depth:
                            queue.append((friend_id, next_depth))
                    edge_key = tuple(sorted((current_id, friend_id)))
                    if edge_key not in edges_seen:
                        edges_seen.add(edge_key)
                        new_edges.append(FriendEdge(from_id=current_id, to_id=friend_id, crawl_id=run.id, source_depth=depth))

                if new_ids:
                    summaries = await self.steam.get_player_summaries(new_ids)
                    by_id = {record.steam_id: record for record in summaries}
                    records: list[SteamUserRecord] = []
                    for steam_id in new_ids:
                        # 少数资料接口缺失的用户仍保留占位节点，关系线不会丢。
                        record = by_id.get(steam_id, placeholder_user(steam_id, discovered[steam_id]))
                        record.depth_min = discovered[steam_id]
                        records.append(record)
                    self.repo.upsert_users(records)
                    nodes_discovered = len(discovered)

                if new_edges:
                    self.repo.upsert_relationships(new_edges)
                    edges_discovered += len(new_edges)

                self.repo.update_crawl_run(
                    run.id,
                    nodes_discovered=nodes_discovered,
                    edges_discovered=edges_discovered,
                    private_count=private_count,
                    error_count=error_count,
                )
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)

            self.repo.update_crawl_run(
                run.id,
                status=CrawlStatus.completed.value,
                finished_at=utc_now_iso(),
                nodes_discovered=len(discovered),
                edges_discovered=edges_discovered,
                private_count=private_count,
                error_count=error_count,
                message="抓取完成",
            )
        except Exception as exc:
            self.repo.update_crawl_run(
                run.id,
                status=CrawlStatus.failed.value,
                finished_at=utc_now_iso(),
                private_count=private_count,
                error_count=error_count + 1,
                message=str(exc),
            )
