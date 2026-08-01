from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from .logs import AppLogBuffer
from .models import (
    CrawlCreate,
    CrawlEvent,
    CrawlRun,
    CrawlStatus,
    FriendEdge,
    FriendListCacheUpdate,
    SteamUserRecord,
    utc_now_iso,
)
from .graph_repo import IGraphRepository
from .steam import SteamApiError, SteamClient, placeholder_user
from .rate_limiter import AdaptiveRateLimiter


@dataclass
class CrawlControl:
    cancel: bool = False
    pause: bool = False
    force_stop: bool = False
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class FriendListLookup:
    steam_id: str
    source: Literal["cache", "api"]
    status: Literal["public", "private", "error"]
    friend_ids: tuple[str, ...] = ()
    error: Exception | None = None


class CrawlManager:
    def __init__(self, repo: IGraphRepository, steam: SteamClient, logs: AppLogBuffer | None = None, project_id: str = "default") -> None:
        self.repo = repo
        self.steam = steam
        self.logs = logs
        self.project_id = project_id
        self.controls: dict[str, CrawlControl] = {}
        self.events: dict[str, list[CrawlEvent]] = {}
        self.event_seq: dict[str, int] = {}
        self.run_history: list[str] = []
        self._lock = asyncio.Lock()
        self._shutting_down = False

    def _gc_completed_runs(self) -> None:
        completed_run_ids = []
        for rid in self.run_history:
            ctrl = self.controls.get(rid)
            if ctrl is None or (ctrl.task is not None and ctrl.task.done()):
                completed_run_ids.append(rid)
        
        if len(completed_run_ids) > 10:
            to_remove = completed_run_ids[:-10]
            for rid in to_remove:
                self.controls.pop(rid, None)
                self.events.pop(rid, None)
                self.event_seq.pop(rid, None)
                if rid in self.run_history:
                    self.run_history.remove(rid)

    def has_active_crawl(self) -> bool:
        for control in self.controls.values():
            if control.task is not None and not control.task.done():
                return True
        return False

    async def shutdown(self, timeout_seconds: float = 15.0) -> None:
        """Stop background crawls before their shared HTTP and database resources close."""
        async with self._lock:
            self._shutting_down = True
            tasks: list[asyncio.Task] = []
            for run_id, control in self.controls.items():
                if control.task is None or control.task.done():
                    continue
                control.force_stop = True
                control.pause = False
                self.append_event(run_id, "warn", "shutdown", "应用正在关闭，停止抓取任务")
                tasks.append(control.task)

        if not tasks:
            return

        _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def create_crawl(self, payload: CrawlCreate) -> CrawlRun:
        async with self._lock:
            if self._shutting_down:
                raise RuntimeError("应用正在关闭，不能创建新的抓取任务。")
            self._gc_completed_runs()
            if self.has_active_crawl():
                raise RuntimeError("已有活跃的抓取任务在运行中，请先停止或等待其完成。")
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
            self.run_history.append(run.id)
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

    async def _load_friend_list_batch(
        self,
        steam_ids: list[str],
        cache_valid_days: int,
    ) -> list[FriendListLookup]:
        lookups: dict[str, FriendListLookup] = {}
        api_ids: list[str] = []
        batch_loader = getattr(self.repo, "get_cached_friend_lists", None)
        if callable(batch_loader):
            cached_lists = batch_loader(steam_ids, cache_valid_days, self.project_id)
        else:
            cached_lists = {}
            for steam_id in dict.fromkeys(steam_ids):
                cached = self.repo.get_cached_friend_list(
                    steam_id, cache_valid_days, self.project_id
                )
                if cached is not None:
                    cached_lists[steam_id] = cached

        for steam_id in steam_ids:
            cached = cached_lists.get(steam_id)
            if cached is None:
                api_ids.append(steam_id)
                continue
            status, friend_ids = cached
            lookups[steam_id] = FriendListLookup(
                steam_id=steam_id,
                source="cache",
                status="public" if status == "public" else "private",
                friend_ids=tuple(friend_ids),
            )

        if api_ids:
            results = await asyncio.gather(
                *(self.steam.get_friend_list(steam_id) for steam_id in api_ids),
                return_exceptions=True,
            )
            for steam_id, result in zip(api_ids, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    if not isinstance(result, Exception):
                        raise RuntimeError(str(result)) from result
                    lookups[steam_id] = FriendListLookup(
                        steam_id=steam_id,
                        source="api",
                        status="error",
                        error=result,
                    )
                    continue
                lookups[steam_id] = FriendListLookup(
                    steam_id=steam_id,
                    source="api",
                    status="private" if result.private else "public",
                    friend_ids=tuple(result.friend_ids),
                )

        return [lookups[steam_id] for steam_id in steam_ids]

    def _persist_api_friend_lists(self, lookups: list[FriendListLookup]) -> None:
        updates = [
            FriendListCacheUpdate(
                steam_id=lookup.steam_id,
                status=lookup.status,
                friend_count=len(lookup.friend_ids) if lookup.status == "public" else None,
                friend_count_status=lookup.status,
                friend_ids=list(lookup.friend_ids),
            )
            for lookup in lookups
            if lookup.source == "api" and lookup.error is None
        ]
        if not updates:
            return
        batch_writer = getattr(self.repo, "mark_friend_list_statuses", None)
        if callable(batch_writer):
            batch_writer(updates, self.project_id)
            return
        for update in updates:
            self.repo.mark_friend_list_status(
                update.steam_id,
                update.status,
                friend_count=update.friend_count,
                friend_count_status=update.friend_count_status or "unknown",
                friend_ids=update.friend_ids or [],
                project_id=self.project_id,
            )

    async def _run_crawl(self, run: CrawlRun, payload: CrawlCreate, control: CrawlControl) -> None:
        def on_delay_change(old_d: float, new_d: float, reason: str):
            reason_cn = "请求成功" if reason == "success" else "发生重试/受限"
            self.append_event(
                run.id, "info", "limiter",
                f"[限速器] {reason_cn}，延迟调整为 {int(new_d)}ms"
            )

        limiter = AdaptiveRateLimiter(
            base_delay_ms=float(payload.delay_ms),
            on_change_callback=on_delay_change
        )
        self.steam.rate_limiter = limiter

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
        consecutive_auth_errors = 0
        nodes_discovered = 0
        edges_discovered = 0
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
                same_pool_edge_keys: set[tuple[str, str]] = set()
                next_depth = depth + 1
                layer_ids = [steam_id for steam_id in sorted(current_layer) if steam_id not in expanded]
                layer_total = len(layer_ids)
                for batch_start in range(0, layer_total, payload.request_concurrency):
                    while control.pause and not control.force_stop:
                        await asyncio.sleep(0.5)
                    if control.force_stop:
                        break
                    batch_ids = layer_ids[batch_start:batch_start + payload.request_concurrency]
                    for batch_offset, current_id in enumerate(batch_ids, start=1):
                        idx = batch_start + batch_offset
                        expanded.add(current_id)
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

                    lookups = await self._load_friend_list_batch(batch_ids, payload.cache_valid_days)
                    if control.force_stop:
                        break
                    self._persist_api_friend_lists(lookups)

                    for lookup in lookups:
                        current_id = lookup.steam_id
                        if lookup.error is not None:
                            if not isinstance(lookup.error, SteamApiError):
                                raise lookup.error
                            exc = lookup.error
                            error_count += 1
                            self.append_event(run.id, "error", "friends", f"[API错误] {current_id}: {exc}")
                            self.repo.update_crawl_run(run.id, error_count=error_count)
                            if exc.status_code in {401, 403}:
                                consecutive_auth_errors += 1
                                if consecutive_auth_errors >= 5:
                                    raise RuntimeError("Steam API 认证失败连续超过 5 次，可能 API Key 已失效，任务熔断退出。")
                            else:
                                consecutive_auth_errors = 0
                            continue

                        if lookup.source == "api":
                            consecutive_auth_errors = 0

                        if lookup.status == "private":
                            private_count += 1
                            self.append_event(run.id, "warn", "private", f"[{'缓存' if lookup.source == 'cache' else 'API'}] 私密: {current_id}")
                            self.repo.update_crawl_run(run.id, private_count=private_count)
                            continue

                        friend_ids = list(lookup.friend_ids)
                        if lookup.source == "api":
                            self.append_event(run.id, "info", "expand", f"  └ API返回: {len(friend_ids)} 位好友")
                        else:
                            self.append_event(run.id, "info", "expand", f"  └ 缓存命中: {len(friend_ids)} 位好友")

                        for friend_id in friend_ids:
                            edge_key = tuple(sorted((current_id, friend_id)))
                            edge = FriendEdge(from_id=current_id, to_id=friend_id, crawl_id=run.id, source_depth=depth)
                            if friend_id in discovered:
                                if edge_key not in edges_seen and edge_key not in same_pool_edge_keys:
                                    same_pool_edge_keys.add(edge_key)
                                    same_pool_edges.append(edge)
                                continue
                            candidate_hits[friend_id].add(current_id)
                            if edge_key not in edges_seen:
                                candidate_edges[friend_id].append(edge)



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

                # ── 阶段性写盘辅助函数 ──
                async def flush_batch(batch_ids: list[str]):
                    if not batch_ids:
                        return
                    nonlocal edges_discovered, nodes_discovered, same_pool_edges
                    batch_summaries = await self.steam.get_player_summaries(batch_ids)
                    by_id_map = {rec.steam_id: rec for rec in batch_summaries}
                    batch_records = []
                    for sid in batch_ids:
                        rec = by_id_map.get(sid, placeholder_user(sid, discovered[sid]))
                        rec.depth_min = discovered[sid]
                        met = candidate_metrics.get(sid, {})
                        rec.friend_count = met.get("friend_count")  # type: ignore[assignment]
                        rec.friend_count_status = str(met.get("friend_count_status", "unknown"))
                        rec.prior_pool_link_count = int(met.get("prior_pool_link_count", 0))
                        rec.root_closeness_score = float(met.get("root_closeness_score", 0))
                        rec.last_scored_crawl_id = str(met.get("last_scored_crawl_id", ""))
                        batch_records.append(rec)
                    
                    self.repo.upsert_users(batch_records, self.project_id)
                    
                    batch_edges = []
                    for sid in batch_ids:
                        for edge in candidate_edges[sid]:
                            edge_key = tuple(sorted((edge.from_id, edge.to_id)))
                            if edge_key not in edges_seen:
                                edges_seen.add(edge_key)
                                batch_edges.append(edge)
                    
                    if same_pool_edges:
                        for edge in same_pool_edges:
                            edge_key = tuple(sorted((edge.from_id, edge.to_id)))
                            if edge_key not in edges_seen:
                                edges_seen.add(edge_key)
                                batch_edges.append(edge)
                        same_pool_edges = []
                        same_pool_edge_keys.clear()
                    
                    if batch_edges:
                        self.repo.upsert_relationships(batch_edges, self.project_id)
                        edges_discovered += len(batch_edges)
                        
                    nodes_discovered = len(discovered)
                    self.repo.update_crawl_run(
                        run.id,
                        nodes_discovered=nodes_discovered,
                        edges_discovered=edges_discovered,
                        private_count=private_count,
                        error_count=error_count,
                        progress_percent=self._progress(nodes_discovered, run.max_nodes, False),
                    )

                pending_batch_ids: list[str] = []

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
                remaining_capacity = max(0, run.max_nodes - len(discovered))
                if len(ordered_candidates) > remaining_capacity:
                    self.append_event(run.id, "warn", "limit", f"已达节点上限 {run.max_nodes}，停止收候选")
                    ordered_candidates = ordered_candidates[:remaining_capacity]

                for batch_start in range(0, len(ordered_candidates), payload.request_concurrency):
                    while control.pause and not control.force_stop:
                        await asyncio.sleep(0.5)
                    if control.force_stop:
                        break

                    batch_ids = ordered_candidates[batch_start:batch_start + payload.request_concurrency]
                    lookups = (
                        await self._load_friend_list_batch(batch_ids, payload.cache_valid_days)
                        if uses_friend_count_filter
                        else []
                    )
                    self._persist_api_friend_lists(lookups)
                    lookup_by_id = {lookup.steam_id: lookup for lookup in lookups}

                    for friend_id in batch_ids:
                        current_layer_links = len(candidate_hits[friend_id])
                        inner_links = cross_links.get(friend_id, 0)
                        total_prior_links = max(current_layer_links, inner_links)
                        skip_deeper = False

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
                            lookup = lookup_by_id[friend_id]
                            friend_count_status = lookup.status
                            if lookup.error is not None:
                                if not isinstance(lookup.error, SteamApiError):
                                    raise lookup.error
                                exc = lookup.error
                                error_count += 1
                                self.append_event(run.id, "error", "friends", f"[API错误] {friend_id}: {exc}")
                                self.repo.update_crawl_run(run.id, error_count=error_count)
                                if exc.status_code in {401, 403}:
                                    consecutive_auth_errors += 1
                                    if consecutive_auth_errors >= 5:
                                        raise RuntimeError("Steam API 认证失败连续超过 5 次，可能 API Key 已失效，任务熔断退出。")
                                else:
                                    consecutive_auth_errors = 0
                            else:
                                if lookup.source == "api":
                                    consecutive_auth_errors = 0
                                if lookup.status == "public":
                                    friend_count = len(lookup.friend_ids)

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

                        pending_batch_ids.append(friend_id)
                        if len(pending_batch_ids) >= 15:
                            await flush_batch(pending_batch_ids)
                            pending_batch_ids = []

                # 写入最后一批剩余的缓冲区
                if pending_batch_ids:
                    await flush_batch(pending_batch_ids)
                    pending_batch_ids = []
                
                # 写入可能遗留的 same_pool_edges
                if same_pool_edges:
                    batch_edges = []
                    for edge in same_pool_edges:
                        edge_key = tuple(sorted((edge.from_id, edge.to_id)))
                        if edge_key not in edges_seen:
                            edges_seen.add(edge_key)
                            batch_edges.append(edge)
                    same_pool_edges = []
                    same_pool_edge_keys.clear()
                    if batch_edges:
                        self.repo.upsert_relationships(batch_edges, self.project_id)
                        edges_discovered += len(batch_edges)

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

                if accepted_ids:
                    active = len(accepted_ids) - len(no_deeper_scan)
                    soft_filtered = len(no_deeper_scan)
                    self.append_event(
                        run.id, "info", "summary",
                        f"深度{depth}→{next_depth}: 收录{len(accepted_ids)}人 (其中{active}人继续展开, {soft_filtered}人标记不展开), 节点总计{len(discovered)}",
                    )
                    self.append_event(run.id, "info", "users", f"已写入用户节点, 总计{nodes_discovered}")
                    self.append_event(run.id, "info", "edges", f"已写入关系线, 关系总计{edges_discovered}")

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
        except asyncio.CancelledError:
            event = self.append_event(run.id, "warn", "stopped", "应用关闭，抓取任务已停止")
            try:
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
            except Exception as exc:
                if self.logs is not None:
                    self.logs.append("error", "crawl:shutdown", f"抓取停止状态写入失败: {exc}")
            raise
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
        finally:
            self.steam.rate_limiter = None

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
