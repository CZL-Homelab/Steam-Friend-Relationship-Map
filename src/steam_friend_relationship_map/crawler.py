from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass

from .logs import AppLogBuffer
from .models import CrawlCreate, CrawlEvent, CrawlRun, CrawlStatus, FriendEdge, SteamUserRecord, utc_now_iso
from .neo4j_repo import Neo4jRepository
from .steam import SteamApiError, SteamClient, placeholder_user


@dataclass
class CrawlControl:
    cancel: bool = False
    pause: bool = False
    force_stop: bool = False
    task: asyncio.Task | None = None


class CrawlManager:
    def __init__(self, repo: Neo4jRepository, steam: SteamClient, logs: AppLogBuffer | None = None, project_id: str = "default") -> None:
        self.repo = repo
        self.steam = steam
        self.logs = logs
        self.project_id = project_id
        self.controls: dict[str, CrawlControl] = {}
        self.events: dict[str, list[CrawlEvent]] = {}
        self.event_seq: dict[str, int] = {}

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
        self.repo.start_crawl_run(run, self.project_id)
        self.events[run.id] = []
        self.event_seq[run.id] = 0
        control = CrawlControl()
        self.controls[run.id] = control
        self.append_event(run.id, "info", "created", "抓取任务已创建")
        control.task = asyncio.create_task(self._run_crawl(run, payload, control))
        return run

    def cancel(self, run_id: str) -> bool:
        """优雅停止：完成当前层后停止，数据保留。"""
        control = self.controls.get(run_id)
        if control is None:
            return False
        control.cancel = True
        self.append_event(run_id, "warn", "cancel", "收到停止请求，将在当前层完成后停止")
        return True

    def force_stop(self, run_id: str) -> bool:
        """强制中断：立即停止，已扫描数据保留。"""
        control = self.controls.get(run_id)
        if control is None:
            return False
        control.force_stop = True
        control.pause = False
        self.append_event(run_id, "warn", "stop", "收到强制中断请求，立即停止（已扫描数据保留）")
        return True

    def pause(self, run_id: str) -> bool:
        """暂停扫描（如遇 Steam 限流）。"""
        control = self.controls.get(run_id)
        if control is None:
            return False
        if control.pause:
            return False
        control.pause = True
        self.repo.update_crawl_run(run_id, status=CrawlStatus.paused.value)
        self.append_event(run_id, "warn", "pause", "扫描已暂停（如 Steam 并发限制），可点击继续")
        return True

    def resume(self, run_id: str) -> bool:
        """继续扫描。"""
        control = self.controls.get(run_id)
        if control is None or not control.pause:
            return False
        control.pause = False
        self.repo.update_crawl_run(run_id, status=CrawlStatus.running.value)
        self.append_event(run_id, "info", "resume", "扫描已继续")
        return True

    def get_events(self, run_id: str, after: int = 0) -> list[CrawlEvent]:
        return [event for event in self.events.get(run_id, []) if event.seq > after]

    def append_event(self, run_id: str, level: str, stage: str, message: str) -> CrawlEvent:
        if self.logs is not None:
            message = self.logs.redact(message)
        seq = self.event_seq.get(run_id, 0) + 1
        self.event_seq[run_id] = seq
        event = CrawlEvent(seq=seq, run_id=run_id, time=utc_now_iso(), level=level, stage=stage, message=message)
        rows = self.events.setdefault(run_id, [])
        rows.append(event)
        del rows[:-300]
        if self.logs is not None:
            self.logs.append(level, f"crawl:{stage}", message)
        return event

    async def _run_crawl(self, run: CrawlRun, payload: CrawlCreate, control: CrawlControl) -> None:
        # 按层处理 BFS，先统计候选人与前层用户池的连接数，再决定是否进入下一层。
        discovered: dict[str, int] = {run.root_steam_id: 0}
        expanded: set[str] = set()
        edges_seen: set[tuple[str, str]] = set()
        current_layer: set[str] = {run.root_steam_id}
        private_count = 0
        error_count = 0
        filtered_count = 0
        friend_count_filtered_count = 0
        prior_pool_filtered_count = 0
        try:
            event = self.append_event(run.id, "info", "root", "正在抓取 Root 用户资料")
            self.repo.update_crawl_run(
                run.id,
                status=CrawlStatus.running.value,
                message=event.message,
                last_event=event.message,
                progress_percent=1,
            )
            root_records = await self.steam.get_player_summaries([run.root_steam_id])
            root = root_records[0] if root_records else placeholder_user(run.root_steam_id, 0)
            root.depth_min = 0
            root.root_closeness_score = 100
            root.last_scored_crawl_id = run.id
            self.repo.upsert_users([root], self.project_id)
            nodes_discovered = 1
            edges_discovered = 0
            self.append_event(run.id, "info", "root", f"Root 用户已写入: {run.root_steam_id}")

            for depth in range(run.max_depth):
                if not current_layer:
                    break

                # ── 暂停检查 ──
                while control.pause and not control.force_stop:
                    await asyncio.sleep(0.5)

                # ── 强制中断：立即停止，数据保留 ──
                if control.force_stop:
                    event = self.append_event(run.id, "warn", "stopped", "用户强制中断（已扫描数据保留）")
                    self.repo.update_crawl_run(
                        run.id,
                        status=CrawlStatus.stopped.value,
                        finished_at=utc_now_iso(),
                        nodes_discovered=nodes_discovered,
                        edges_discovered=edges_discovered,
                        private_count=private_count,
                        error_count=error_count,
                        filtered_count=filtered_count,
                        friend_count_filtered_count=friend_count_filtered_count,
                        prior_pool_filtered_count=prior_pool_filtered_count,
                        message=event.message,
                        last_event=event.message,
                    )
                    return

                # ── 优雅停止：完成当前层 ──
                if control.cancel:
                    event = self.append_event(run.id, "warn", "cancelled", "用户停止扫描，完成当前层后停止（数据保留）")
                    self.repo.update_crawl_run(
                        run.id,
                        status=CrawlStatus.cancelled.value,
                        finished_at=utc_now_iso(),
                        nodes_discovered=nodes_discovered,
                        edges_discovered=edges_discovered,
                        private_count=private_count,
                        error_count=error_count,
                        filtered_count=filtered_count,
                        friend_count_filtered_count=friend_count_filtered_count,
                        prior_pool_filtered_count=prior_pool_filtered_count,
                        message=event.message,
                        last_event=event.message,
                    )
                    return

                candidate_hits: dict[str, set[str]] = defaultdict(set)
                candidate_edges: dict[str, list[FriendEdge]] = defaultdict(list)
                same_pool_edges: list[FriendEdge] = []
                next_depth = depth + 1
                for idx, current_id in enumerate(sorted(current_layer), start=1):
                    # ── 内部暂停/强制中断检查 ──
                    while control.pause and not control.force_stop:
                        await asyncio.sleep(0.5)
                    if control.force_stop:
                        break
                    if current_id in expanded:
                        continue
                    expanded.add(current_id)
                    layer_total = len(current_layer)
                    root_name = root.persona_name or root.steam_id
                    self.append_event(
                        run.id, "info", "expand",
                        f"深度{depth} 第{idx}/{layer_total}个: {current_id} (节点总计{len(discovered)})",
                    )
                    self.repo.update_crawl_run(
                        run.id,
                        current_depth=depth,
                        current_steam_id=current_id,
                        queue_size=layer_total - idx,
                        expanded_count=len(expanded),
                        nodes_discovered=len(discovered),
                        progress_percent=self._progress(len(discovered), run.max_nodes, False),
                    )

                    cached = self.repo.get_cached_friend_list(current_id, payload.cache_valid_days, self.project_id)
                    used_cache = False
                    if cached is not None:
                        status, cached_ids = cached
                        if status == "private":
                            private_count += 1
                            self.append_event(run.id, "warn", "private", f"[缓存] 私密: {current_id}")
                            self.repo.update_crawl_run(run.id, private_count=private_count)
                            continue
                        friend_ids = cached_ids
                        used_cache = True
                        self.append_event(run.id, "info", "expand", f"  └ 缓存命中: {len(friend_ids)} 位好友")
                    else:
                        try:
                            friends = await self.steam.get_friend_list(current_id)
                        except SteamApiError as exc:
                            error_count += 1
                            self.append_event(run.id, "error", "friends", f"[API错误] {current_id}: {exc}")
                            self.repo.update_crawl_run(run.id, error_count=error_count)
                            continue

                        if friends.private:
                            private_count += 1
                            self.repo.mark_friend_list_status(current_id, "private", friend_count=None, friend_count_status="private", project_id=self.project_id)
                            self.append_event(run.id, "warn", "private", f"[API] 私密: {current_id}")
                            self.repo.update_crawl_run(run.id, private_count=private_count)
                            continue

                        self.repo.mark_friend_list_status(current_id, "public", friend_count=len(friends.friend_ids), friend_count_status="public", project_id=self.project_id)
                        friend_ids = friends.friend_ids
                        self.append_event(run.id, "info", "expand", f"  └ API返回: {len(friend_ids)} 位好友")

                    for friend_id in friend_ids:
                        edge_key = tuple(sorted((current_id, friend_id)))
                        edge = FriendEdge(from_id=current_id, to_id=friend_id, crawl_id=run.id, source_depth=depth)
                        if friend_id in discovered:
                            if edge_key not in edges_seen:
                                edges_seen.add(edge_key)
                                same_pool_edges.append(edge)
                            continue
                        candidate_hits[friend_id].add(current_id)
                        if edge_key not in edges_seen:
                            candidate_edges[friend_id].append(edge)

                    if not used_cache and payload.delay_ms:
                        await asyncio.sleep(payload.delay_ms / 1000)

                # ── 内层循环后再次检查强制中断 ──
                if control.force_stop:
                    event = self.append_event(run.id, "warn", "stopped", "用户强制中断（已扫描数据保留）")
                    self.repo.update_crawl_run(
                        run.id,
                        status=CrawlStatus.stopped.value,
                        finished_at=utc_now_iso(),
                        nodes_discovered=len(discovered),
                        edges_discovered=edges_discovered,
                        private_count=private_count,
                        error_count=error_count,
                        filtered_count=filtered_count,
                        friend_count_filtered_count=friend_count_filtered_count,
                        prior_pool_filtered_count=prior_pool_filtered_count,
                        message=event.message,
                        last_event=event.message,
                    )
                    return

                accepted_ids: list[str] = []
                candidate_metrics: dict[str, dict[str, object]] = {}
                no_deeper_scan: set[str] = set()
                uses_friend_count_filter = payload.friend_count_min is not None or payload.friend_count_max is not None

                # ── 跨层前层连接统计 ──
                cross_links: dict[str, int] = {}
                if payload.prior_pool_min_links:
                    inner_pool = [sid for sid, d in discovered.items() if d <= depth]
                    if inner_pool and candidate_hits:
                        cross_links = self.repo.count_inner_layer_links(
                            list(candidate_hits.keys()), inner_pool, self.project_id,
                        )
                        self.append_event(
                            run.id, "info", "filter",
                            f"跨层连接查询完成: {len(cross_links)} 位候选与内层 {len(inner_pool)} 用户有连接",
                        )

                ordered_candidates = sorted(candidate_hits, key=lambda steam_id: (-len(candidate_hits[steam_id]), steam_id))
                for friend_id in ordered_candidates:
                    if len(discovered) >= run.max_nodes:
                        self.append_event(run.id, "warn", "limit", f"已达节点上限 {run.max_nodes}，停止收候选")
                        break

                    current_layer_links = len(candidate_hits[friend_id])
                    inner_links = cross_links.get(friend_id, 0)
                    total_prior_links = max(current_layer_links, inner_links)

                    # ── 判断是否阻止深层扫描 ──
                    skip_deeper = False

                    # 跨层连接检查：不通过则收录但标记为孤岛，不再展开
                    if payload.prior_pool_min_links and total_prior_links < payload.prior_pool_min_links:
                        prior_pool_filtered_count += 1
                        filtered_count += 1
                        skip_deeper = True
                        self.append_event(
                            run.id, "warn", "filter",
                            f"孤立节点-收录但不展开: {friend_id} (跨层连接={total_prior_links} < 需要≥{payload.prior_pool_min_links}), 已与前面用户形成'孤岛'",
                        )

                    friend_count: int | None = None
                    friend_count_status = "unknown"
                    if uses_friend_count_filter:
                        cached_candidate = self.repo.get_cached_friend_list(friend_id, payload.cache_valid_days, self.project_id)
                        if cached_candidate is not None:
                            c_status, c_ids = cached_candidate
                            friend_count_status = c_status
                            if c_status == "public":
                                friend_count = len(c_ids)
                        else:
                            try:
                                candidate_friends = await self.steam.get_friend_list(friend_id)
                            except SteamApiError:
                                friend_count_status = "error"
                            else:
                                if candidate_friends.private:
                                    friend_count_status = "private"
                                    self.repo.mark_friend_list_status(friend_id, "private", friend_count=None, friend_count_status="private", project_id=self.project_id)
                                else:
                                    friend_count_status = "public"
                                    friend_count = len(candidate_friends.friend_ids)
                                    self.repo.mark_friend_list_status(friend_id, "public", friend_count=friend_count, friend_count_status="public", project_id=self.project_id)
                            if payload.delay_ms:
                                await asyncio.sleep(payload.delay_ms / 1000)

                        if not self._friend_count_matches(friend_count, friend_count_status, payload):
                            friend_count_filtered_count += 1
                            filtered_count += 1
                            skip_deeper = True
                            self.append_event(
                                run.id, "warn", "filter",
                                f"好友数超限-收录但不展开: {friend_id} (好友数={friend_count or '?'}, 范围 {payload.friend_count_min or 0}~{payload.friend_count_max or '∞'}), 该用户将不参与更深层扫描!",
                            )

                    discovered[friend_id] = next_depth
                    accepted_ids.append(friend_id)
                    if skip_deeper:
                        no_deeper_scan.add(friend_id)

                    label = "收录(不展开)" if skip_deeper else "收录"
                    self.append_event(
                        run.id, "info", "accept",
                        f"{label}: {friend_id} @深度{next_depth} (前层连接={total_prior_links}, 好友数={friend_count or '?'})",
                    )
                    candidate_metrics[friend_id] = {
                        "friend_count": friend_count,
                        "friend_count_status": friend_count_status,
                        "prior_pool_link_count": total_prior_links,
                        "root_closeness_score": self._score(next_depth, total_prior_links, friend_count),
                        "last_scored_crawl_id": run.id,
                    }

                new_edges: list[FriendEdge] = same_pool_edges[:]
                for friend_id in accepted_ids:
                    for edge in candidate_edges[friend_id]:
                        edge_key = tuple(sorted((edge.from_id, edge.to_id)))
                        if edge_key not in edges_seen:
                            edges_seen.add(edge_key)
                            new_edges.append(edge)

                if accepted_ids:
                    active = len(current_layer)
                    soft_filtered = len(accepted_ids) - active
                    self.append_event(
                        run.id, "info", "summary",
                        f"深度{depth}→{next_depth}: 收录{len(accepted_ids)}人 (其中{active}人继续展开, {soft_filtered}人标记不展开), 节点总计{len(discovered)}",
                    )
                    summaries = await self.steam.get_player_summaries(accepted_ids)
                    by_id = {record.steam_id: record for record in summaries}
                    records: list[SteamUserRecord] = []
                    for steam_id in accepted_ids:
                        # 少数资料接口缺失的用户仍保留占位节点，关系线不会丢。
                        record = by_id.get(steam_id, placeholder_user(steam_id, discovered[steam_id]))
                        record.depth_min = discovered[steam_id]
                        metrics = candidate_metrics.get(steam_id, {})
                        record.friend_count = metrics.get("friend_count")  # type: ignore[assignment]
                        record.friend_count_status = str(metrics.get("friend_count_status", "unknown"))
                        record.prior_pool_link_count = int(metrics.get("prior_pool_link_count", 0))
                        record.root_closeness_score = float(metrics.get("root_closeness_score", 0))
                        record.last_scored_crawl_id = str(metrics.get("last_scored_crawl_id", ""))
                        records.append(record)
                    self.repo.upsert_users(records, self.project_id)
                    nodes_discovered = len(discovered)
                    self.append_event(run.id, "info", "users", f"已写入用户节点, 总计{nodes_discovered}")

                if new_edges:
                    self.repo.upsert_relationships(new_edges, self.project_id)
                    edges_discovered += len(new_edges)
                    self.append_event(run.id, "info", "edges", f"已写入{len(new_edges)}条关系, 总计{edges_discovered}")

                self.repo.update_crawl_run(
                    run.id,
                    nodes_discovered=nodes_discovered,
                    edges_discovered=edges_discovered,
                    private_count=private_count,
                    error_count=error_count,
                    queue_size=len(accepted_ids),
                    expanded_count=len(expanded),
                    filtered_count=filtered_count,
                    friend_count_filtered_count=friend_count_filtered_count,
                    prior_pool_filtered_count=prior_pool_filtered_count,
                    progress_percent=self._progress(nodes_discovered, run.max_nodes, False),
                )
                current_layer = {sid for sid in accepted_ids if sid not in no_deeper_scan}

            event = self.append_event(
                run.id, "info", "completed",
                f"抓取完成! 节点{len(discovered)} 关系{edges_discovered} 私密{private_count} 错误{error_count} 筛选{filtered_count}",
            )
            self.repo.update_crawl_run(
                run.id,
                status=CrawlStatus.completed.value,
                finished_at=utc_now_iso(),
                nodes_discovered=len(discovered),
                edges_discovered=edges_discovered,
                private_count=private_count,
                error_count=error_count,
                filtered_count=filtered_count,
                friend_count_filtered_count=friend_count_filtered_count,
                prior_pool_filtered_count=prior_pool_filtered_count,
                progress_percent=100,
                queue_size=0,
                expanded_count=len(expanded),
                message=event.message,
                last_event=event.message,
            )
        except Exception as exc:
            event = self.append_event(run.id, "error", "failed", str(exc))
            self.repo.update_crawl_run(
                run.id,
                status=CrawlStatus.failed.value,
                finished_at=utc_now_iso(),
                private_count=private_count,
                error_count=error_count + 1,
                filtered_count=filtered_count,
                friend_count_filtered_count=friend_count_filtered_count,
                prior_pool_filtered_count=prior_pool_filtered_count,
                message=str(exc),
                last_event=event.message,
            )

    @staticmethod
    def _friend_count_matches(friend_count: int | None, status: str, payload: CrawlCreate) -> bool:
        if payload.friend_count_min is None and payload.friend_count_max is None:
            return True
        if status != "public" or friend_count is None:
            return False
        if payload.friend_count_min is not None and friend_count < payload.friend_count_min:
            return False
        if payload.friend_count_max is not None and friend_count > payload.friend_count_max:
            return False
        return True

    @staticmethod
    def _score(depth: int, prior_links: int, friend_count: int | None) -> float:
        friend_factor = min(friend_count or 0, 2000) / 100
        return round(prior_links * 10 + friend_factor - depth * 3, 2)

    @staticmethod
    def _progress(nodes_discovered: int, max_nodes: int, done: bool) -> int:
        if done:
            return 100
        if max_nodes <= 0:
            return 1
        return max(1, min(99, int(nodes_discovered / max_nodes * 100)))
