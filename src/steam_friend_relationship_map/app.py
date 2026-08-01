from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from dotenv import set_key
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from neo4j.exceptions import AuthError, ServiceUnavailable

from .analytics import analyze_network
from .crawler import CrawlManager
from .logs import AppLogBuffer, install_log_handler, release_log_handler
from .models import (
    AppLog,
    CrawlCreate,
    CrawlEvent,
    CrawlRun,
    DbStats,
    ExportRequest,
    ExportResponse,
    FriendEdge,
    FriendCircleAnalysisResponse,
    GraphNode,
    GraphResponse,
    HealthResponse,
    NetworkAnalysisResponse,
    ProjectCreate,
    ProjectInfo,
    ProjectListResponse,
    ProjectSwitch,
    PublicSettings,
    SecretUpdate,
    SettingsPatch,
    SettingsSave,
    SettingsTestResult,
    SteamUserRecord,
    UserPatch,
    utc_now_iso,
)
from .graph_repo import IGraphRepository
from .neo4j_repo import Neo4jRepositoryImpl
from .kuzu_repo import KuzuRepositoryImpl
from .secrets import SecretStorageError, SecretStore
from .settings import Settings, clear_settings_cache, get_settings
from .steam import SteamApiError, SteamClient


STATIC_DIR = Path(__file__).parent / "static"
ENV_PATH = Path.cwd() / ".env"
ENV_KEYS = {
    "graph_db_engine": "GRAPH_DB_ENGINE",
    "kuzu_db_path": "KUZU_DB_PATH",
    "kuzu_buffer_pool_size_gb": "KUZU_BUFFER_POOL_SIZE_GB",
    "neo4j_uri": "NEO4J_URI",
    "neo4j_user": "NEO4J_USER",
    "app_host": "APP_HOST",
    "app_port": "APP_PORT",
    "default_max_depth": "DEFAULT_MAX_DEPTH",
    "default_max_nodes": "DEFAULT_MAX_NODES",
    "default_delay_ms": "DEFAULT_DELAY_MS",
    "default_cache_valid_days": "DEFAULT_CACHE_VALID_DAYS",
    "active_project": "ACTIVE_PROJECT",
}
CSV_EXPORT_FIELDS = (
    "type",
    "project_id",
    "id",
    "label",
    "source",
    "target",
    "profile_url",
    "avatar",
    "note",
    "tags",
    "category",
    "depth",
    "friend_count",
    "friend_list_status",
    "prior_pool_link_count",
    "root_closeness_score",
)
CSV_EXPORT_CHUNK_SIZE = 64 * 1024


def sanitize_env_value(value: object) -> str:
    return str(value).replace("\n", "").replace("\r", "")


def csv_safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


def iter_export_csv(
    data: ExportResponse,
    project_id: str,
    chunk_size: int = CSV_EXPORT_CHUNK_SIZE,
) -> Iterable[str]:
    """Yield bounded CSV chunks without materializing a second full export copy."""
    buffer = io.StringIO(newline="")
    buffer.write("\ufeff")
    writer = csv.DictWriter(buffer, fieldnames=CSV_EXPORT_FIELDS)
    writer.writeheader()

    def flush() -> str:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk

    for node in data.nodes:
        row = {
            "type": "node",
            "project_id": node.get("project_id", project_id),
            "id": node.get("steam_id", ""),
            "label": node.get("persona_name", ""),
            "source": "",
            "target": "",
            "profile_url": node.get("profile_url", ""),
            "avatar": node.get("avatar_full") or node.get("avatar_medium") or node.get("avatar", ""),
            "note": node.get("note", ""),
            "tags": json.dumps(node.get("tags") or [], ensure_ascii=False),
            "category": node.get("category", ""),
            "depth": node.get("depth_min"),
            "friend_count": node.get("friend_count"),
            "friend_list_status": node.get("friend_list_status", "unknown"),
            "prior_pool_link_count": node.get("prior_pool_link_count", 0),
            "root_closeness_score": node.get("root_closeness_score", 0),
        }
        writer.writerow({key: csv_safe_cell(row.get(key)) for key in CSV_EXPORT_FIELDS})
        if buffer.tell() >= chunk_size:
            yield flush()

    for edge in data.edges:
        row = {
            "type": "edge",
            "project_id": project_id,
            "source": edge["source"],
            "target": edge["target"],
        }
        writer.writerow({key: csv_safe_cell(row.get(key)) for key in CSV_EXPORT_FIELDS})
        if buffer.tell() >= chunk_size:
            yield flush()

    if buffer.tell():
        yield flush()


def sensitive_setting_values(settings: Settings) -> list[str]:
    api_keys = [k.strip() for k in re.split(r"[\s,;]+", settings.steam_api_key) if k.strip()]
    return api_keys + [value for value in (settings.neo4j_password, settings.steam_proxy_url) if value]


def get_repository(settings: Settings) -> IGraphRepository:
    engine = settings.graph_db_engine.lower()
    if engine == "kuzu":
        return KuzuRepositoryImpl(
            db_path=settings.kuzu_db_path,
            buffer_pool_size_gb=settings.kuzu_buffer_pool_size_gb,
        )
    elif engine == "neo4j":
        return Neo4jRepositoryImpl(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
        )
    else:
        raise ValueError(f"Unsupported graph database engine: {engine}")


def repository_settings_changed(old: Settings, new: Settings) -> bool:
    return (
        old.graph_db_engine != new.graph_db_engine
        or old.kuzu_db_path != new.kuzu_db_path
        or old.kuzu_buffer_pool_size_gb != new.kuzu_buffer_pool_size_gb
        or old.neo4j_uri != new.neo4j_uri
        or old.neo4j_user != new.neo4j_user
        or old.neo4j_password != new.neo4j_password
    )


def uses_same_kuzu_database(old: Settings, new: Settings) -> bool:
    if old.graph_db_engine.lower() != "kuzu" or new.graph_db_engine.lower() != "kuzu":
        return False
    return Path(old.kuzu_db_path).resolve() == Path(new.kuzu_db_path).resolve()


class UnavailableRepository(IGraphRepository):
    """Placeholder used when the configured graph database cannot be opened."""

    def __init__(self, exc: Exception) -> None:
        self.message = str(exc)

    def _raise(self) -> None:
        raise RuntimeError(f"Graph database is unavailable: {self.message}")

    def close(self) -> None:
        pass

    def test_connection(self) -> str:
        self._raise()

    def ensure_schema(self) -> None:
        self._raise()

    def list_projects(self) -> ProjectListResponse:
        self._raise()

    def create_project(self, payload: ProjectCreate, project_id: str | None = None) -> str:
        self._raise()

    def delete_project(self, project_id: str) -> bool:
        self._raise()

    def project_exists(self, project_id: str) -> bool:
        self._raise()

    def get_crawl_run(self, run_id: str) -> CrawlRun | None:
        self._raise()

    def start_crawl_run(self, run: CrawlRun, project_id: str) -> None:
        self._raise()

    def update_crawl_run(self, run_id: str, **fields: Any) -> None:
        self._raise()

    def upsert_users(self, users: Iterable[SteamUserRecord], project_id: str) -> None:
        self._raise()

    def mark_friend_list_status(
        self,
        steam_id: str,
        status: str,
        friend_count: int | None,
        friend_count_status: str,
        friend_ids: list[str],
        project_id: str,
    ) -> None:
        self._raise()

    def get_cached_friend_list(
        self, steam_id: str, valid_days: int, project_id: str
    ) -> tuple[str, list[str]] | None:
        self._raise()

    def upsert_relationships(self, edges: Iterable[FriendEdge], project_id: str) -> None:
        self._raise()

    def patch_user(
        self,
        steam_id: str,
        *,
        note: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
        project_id: str = "default",
    ) -> None:
        self._raise()

    def bulk_patch_users(
        self, patches: Iterable[dict[str, Any]], project_id: str = "default"
    ) -> None:
        self._raise()

    def count_inner_layer_links(
        self, candidate_ids: list[str], inner_pool_ids: list[str], project_id: str
    ) -> dict[str, int]:
        self._raise()

    def get_graph(
        self,
        *,
        root: str | None,
        depth: int,
        limit: int,
        query: str | None = None,
        category: str | None = None,
        friend_count_min: int | None = None,
        friend_count_max: int | None = None,
        prior_pool_min_links: int = 0,
        sort_by: str = "depth",
        sort_dir: str = "asc",
        project_id: str = "default",
    ) -> GraphResponse:
        self._raise()

    def get_shortest_path(
        self, from_id: str, to_id: str, max_depth: int, project_id: str = "default"
    ) -> GraphResponse:
        self._raise()

    def get_friend_circle_analysis(
        self,
        root: str,
        max_depth: int = 3,
        min_mutual: int = 2,
        limit: int = 50,
        project_id: str = "default",
    ) -> FriendCircleAnalysisResponse:
        self._raise()

    def get_top_degree(self, limit: int = 12, project_id: str = "default") -> list[GraphNode]:
        self._raise()

    def get_db_stats(self, project_id: str = "default") -> DbStats:
        self._raise()

    def export_graph(self, project_id: str = "default") -> ExportResponse:
        self._raise()


def create_app(
    settings: Settings | None = None,
    repo: IGraphRepository | None = None,
    steam: SteamClient | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    provided_repo = repo
    provided_steam = steam
    provided_settings = settings is not None
    settings = settings or get_settings()
    secret_store = secret_store or SecretStore()
    log_buffer = AppLogBuffer()
    log_buffer.set_secret_values(sensitive_setting_values(settings))
    log_handler = install_log_handler(log_buffer)
    if repo is None:
        try:
            repo = get_repository(settings)
        except Exception as exc:
            log_buffer.append("error", "database", f"Graph database open failed: {exc}")
            repo = UnavailableRepository(exc)
    try:
        repo.ensure_schema()
    except Exception as exc:
        log_buffer.append("warn", "database", f"数据库 Schema 初始化失败: {exc}")
    else:
        try:
            recovery = getattr(repo, "recover_interrupted_crawls", None)
            interrupted = recovery() if callable(recovery) else 0
            if interrupted:
                log_buffer.append(
                    "warn",
                    "crawl:recovery",
                    f"已将 {interrupted} 个上次启动中断的抓取任务标记为已停止",
                )
        except Exception as exc:
            log_buffer.append("warn", "crawl:recovery", f"中断抓取状态恢复失败: {exc}")
    steam = steam or SteamClient(settings.steam_api_key, proxy_url=settings.steam_proxy_url)
    manager = CrawlManager(repo, steam, log_buffer, project_id=settings.active_project)
    runtime_mutation_lock = asyncio.Lock()
    network_analysis_lock = asyncio.Lock()
    started_runtime_mutations: set[asyncio.Task[Any]] = set()

    async def run_runtime_operation(operation, source: str) -> Any:  # type: ignore[no-untyped-def]
        """Keep a started runtime read protected until all async or worker work finishes."""
        started = asyncio.Event()

        async def guarded() -> Any:
            async with runtime_mutation_lock:
                started.set()
                return await operation()

        task = asyncio.create_task(guarded())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not started.is_set():
                task.cancel()
            else:
                def report_late_failure(completed: asyncio.Task[Any]) -> None:
                    try:
                        completed.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        log_buffer.append(
                            "warn",
                            "runtime",
                            f"Cancelled {source} later failed: {log_buffer.redact(str(exc))}",
                        )

                task.add_done_callback(report_late_failure)
            raise

    async def call_repository(
        method_name: str,
        *args: Any,
        project_scoped: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Run synchronous repository work off-loop while guarding runtime swaps."""
        async def operation() -> Any:
            if project_scoped:
                kwargs["project_id"] = settings.active_project
            method = getattr(repo, method_name)
            return await asyncio.to_thread(method, *args, **kwargs)

        return await run_runtime_operation(operation, "repository operation")

    async def calculate_network_analysis(
        data: ExportResponse,
        *,
        limit: int,
        resolution: float,
    ) -> NetworkAnalysisResponse:
        """Run at most one non-cancellable NetworkX worker at a time."""
        started = asyncio.Event()

        async def operation() -> NetworkAnalysisResponse:
            async with network_analysis_lock:
                started.set()
                return await asyncio.to_thread(
                    analyze_network,
                    data,
                    limit=limit,
                    resolution=resolution,
                )

        task = asyncio.create_task(operation())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not started.is_set():
                task.cancel()
            else:
                def report_late_failure(completed: asyncio.Task[NetworkAnalysisResponse]) -> None:
                    try:
                        completed.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        log_buffer.append(
                            "warn",
                            "analysis",
                            "Cancelled network analysis later failed: "
                            f"{log_buffer.redact(str(exc))}",
                        )

                task.add_done_callback(report_late_failure)
            raise

    @asynccontextmanager
    async def runtime_mutation_guard() -> AsyncIterator[None]:
        async with runtime_mutation_lock:
            if manager.has_active_crawl():
                raise HTTPException(
                    status_code=400,
                    detail="当前有活跃的抓取任务在运行，请先停止任务后再修改运行时状态。",
                )
            current_task = asyncio.current_task()
            if current_task is not None:
                started_runtime_mutations.add(current_task)
            yield

    def finish_runtime_mutation(source: str):  # type: ignore[no-untyped-def]
        """Let started writes finish, but cancel writes still waiting for the lock."""
        def decorator(function):  # type: ignore[no-untyped-def]
            @wraps(function)
            async def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
                async def operation():  # type: ignore[no-untyped-def]
                    try:
                        return await function(*args, **kwargs)
                    finally:
                        current_task = asyncio.current_task()
                        if current_task is not None:
                            started_runtime_mutations.discard(current_task)

                task = asyncio.create_task(operation())
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task not in started_runtime_mutations:
                        task.cancel()

                    def report_late_failure(completed: asyncio.Task[Any]) -> None:
                        try:
                            completed.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            log_buffer.append(
                                "error",
                                source,
                                f"Cancelled runtime mutation later failed: {log_buffer.redact(str(exc))}",
                            )

                    task.add_done_callback(report_late_failure)
                    raise

            return wrapped

        return decorator

    async def restore_env_settings(values: dict[str, Any]) -> list[str]:
        errors = []
        for field, value in values.items():
            try:
                await asyncio.to_thread(
                    set_key,
                    str(ENV_PATH),
                    ENV_KEYS[field],
                    sanitize_env_value(value),
                    quote_mode="never",
                )
            except Exception as exc:
                errors.append(f"{field}: {exc}")
        clear_settings_cache()
        return errors

    async def persist_env_settings(values: dict[str, Any]) -> None:
        if not values:
            return
        await asyncio.to_thread(ENV_PATH.touch, exist_ok=True)
        for field, value in values.items():
            await asyncio.to_thread(
                set_key,
                str(ENV_PATH),
                ENV_KEYS[field],
                sanitize_env_value(value),
                quote_mode="never",
            )

    async def read_secret_settings(names: Iterable[str]) -> dict[str, str]:
        def read() -> dict[str, str]:
            return {name: secret_store.get(name) for name in names}

        return await asyncio.to_thread(read)

    async def persist_secret_settings(values: dict[str, str]) -> None:
        def persist() -> None:
            for name, value in values.items():
                secret_store.set(name, value)

        await asyncio.to_thread(persist)

    async def restore_secret_settings(values: dict[str, str]) -> list[str]:
        def restore() -> list[str]:
            errors = []
            for name, value in values.items():
                try:
                    if value:
                        secret_store.set(name, value)
                    else:
                        secret_store.delete(name)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            return errors

        errors = await asyncio.to_thread(restore)
        clear_settings_cache()
        return errors

    async def apply_active_project(project_id: str) -> None:
        """Persist and apply the active project while the runtime mutation lock is held."""
        await asyncio.to_thread(ENV_PATH.touch, exist_ok=True)
        await asyncio.to_thread(
            set_key,
            str(ENV_PATH),
            "ACTIVE_PROJECT",
            sanitize_env_value(project_id),
            quote_mode="never",
        )
        await rebuild_runtime()

    async def rebuild_runtime() -> None:
        nonlocal settings, repo, steam, manager
        old_settings = settings
        old_repo = repo
        old_steam = steam

        def load_settings() -> Settings:
            clear_settings_cache()
            return get_settings()

        new_settings = await asyncio.to_thread(load_settings)
        log_buffer.set_secret_values(
            sensitive_setting_values(old_settings) + sensitive_setting_values(new_settings)
        )
        should_replace_repo = (
            provided_repo is None
            and repository_settings_changed(old_settings, new_settings)
        )
        should_replace_steam = (
            provided_steam is None
            and (
                old_settings.steam_api_key != new_settings.steam_api_key
                or old_settings.steam_proxy_url != new_settings.steam_proxy_url
            )
        )

        candidate_repo: IGraphRepository | None = None
        candidate_steam: SteamClient | None = None
        old_repo_closed = False
        try:
            if should_replace_repo:
                if uses_same_kuzu_database(old_settings, new_settings) and not isinstance(
                    old_repo, UnavailableRepository
                ):
                    old_repo_closed = True
                    await asyncio.to_thread(old_repo.close)
                candidate_repo = await asyncio.to_thread(get_repository, new_settings)
                await asyncio.to_thread(candidate_repo.ensure_schema)
            if should_replace_steam:
                candidate_steam = SteamClient(
                    new_settings.steam_api_key,
                    proxy_url=new_settings.steam_proxy_url,
                )
            next_repo = candidate_repo if candidate_repo is not None else old_repo
            next_steam = candidate_steam if candidate_steam is not None else old_steam
            next_manager = CrawlManager(
                next_repo,
                next_steam,
                log_buffer,
                project_id=new_settings.active_project,
            )
        except Exception as exc:
            detail = log_buffer.redact(str(exc))
            cleanup_errors = []
            if candidate_steam is not None:
                try:
                    await candidate_steam.aclose()
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"Steam candidate: {cleanup_exc}")
            if candidate_repo is not None:
                try:
                    await asyncio.to_thread(candidate_repo.close)
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"database candidate: {cleanup_exc}")

            restored_repo = old_repo
            if old_repo_closed:
                try:
                    restored_repo = await asyncio.to_thread(get_repository, old_settings)
                    await asyncio.to_thread(restored_repo.ensure_schema)
                except Exception as restore_exc:
                    restore_detail = log_buffer.redact(str(restore_exc))
                    cleanup_errors.append(f"previous database: {restore_detail}")
                    restored_repo = UnavailableRepository(restore_exc)

            settings = old_settings
            repo = restored_repo
            steam = old_steam
            manager = CrawlManager(repo, steam, log_buffer, project_id=settings.active_project)
            app.state.repo = repo
            app.state.steam = steam
            app.state.manager = manager
            log_buffer.append("error", "runtime", f"Runtime configuration rejected: {detail}")
            if cleanup_errors:
                log_buffer.append(
                    "error",
                    "runtime",
                    f"Runtime rollback was incomplete: {'; '.join(cleanup_errors)}",
                )
            log_buffer.set_secret_values(sensitive_setting_values(settings))
            raise RuntimeError(f"Runtime configuration was not applied: {detail}") from exc

        settings = new_settings
        repo = next_repo
        steam = next_steam
        manager = next_manager
        app.state.repo = repo
        app.state.steam = steam
        app.state.manager = manager

        if not should_replace_repo:
            try:
                await asyncio.to_thread(repo.ensure_schema)
            except Exception as exc:
                log_buffer.append("warn", "database", f"数据库 Schema 初始化失败: {exc}")
        elif not old_repo_closed:
            try:
                await asyncio.to_thread(old_repo.close)
            except Exception as exc:
                log_buffer.append(
                    "warn",
                    "database",
                    f"Previous graph database cleanup failed after switch: {exc}",
                )

        if should_replace_steam:
            try:
                await old_steam.aclose()
            except Exception as exc:
                log_buffer.append(
                    "warn",
                    "steam",
                    f"Previous Steam client cleanup failed after switch: {exc}",
                )
        log_buffer.set_secret_values(sensitive_setting_values(settings))

    async def public_settings(message: str = "") -> PublicSettings:
        current_settings = settings
        non_secret_settings = current_settings if provided_settings else await asyncio.to_thread(Settings)

        def load_secrets() -> tuple[str, str, str]:
            return (
                secret_store.get("steam_api_key"),
                secret_store.get("steam_proxy_url"),
                secret_store.get("neo4j_password"),
            )

        try:
            steam_secret, proxy_secret, neo4j_secret = await asyncio.to_thread(load_secrets)
            secure_store_available = True
        except SecretStorageError as exc:
            steam_secret = ""
            proxy_secret = ""
            neo4j_secret = ""
            secure_store_available = False
            message = message or str(exc)
        return PublicSettings(
            graph_db_engine=current_settings.graph_db_engine,
            kuzu_db_path=current_settings.kuzu_db_path,
            kuzu_buffer_pool_size_gb=current_settings.kuzu_buffer_pool_size_gb,
            neo4j_uri=current_settings.neo4j_uri,
            neo4j_user=current_settings.neo4j_user,
            app_host=current_settings.app_host,
            app_port=current_settings.app_port,
            default_max_depth=current_settings.default_max_depth,
            default_max_nodes=current_settings.default_max_nodes,
            default_delay_ms=current_settings.default_delay_ms,
            default_cache_valid_days=current_settings.default_cache_valid_days,
            active_project=current_settings.active_project,
            steam_api_key_configured=bool(steam_secret or non_secret_settings.steam_api_key),
            steam_proxy_configured=bool(proxy_secret or non_secret_settings.steam_proxy_url),
            neo4j_password_configured=bool(neo4j_secret or non_secret_settings.neo4j_password),
            steam_api_key_from_env=not bool(steam_secret) and bool(non_secret_settings.steam_api_key),
            steam_proxy_from_env=not bool(proxy_secret) and bool(non_secret_settings.steam_proxy_url),
            neo4j_password_from_env=not bool(neo4j_secret) and bool(non_secret_settings.neo4j_password),
            secure_store_available=secure_store_available,
            message=message,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            try:
                try:
                    async with runtime_mutation_lock:
                        await manager.shutdown()
                finally:
                    try:
                        await steam.aclose()
                    finally:
                        await asyncio.to_thread(repo.close)
            finally:
                release_log_handler(log_handler, log_buffer)

    app = FastAPI(title="Steam Friend Relationship Map", lifespan=lifespan)
    app.state.repo = repo
    app.state.steam = steam
    app.state.manager = manager
    app.state.runtime_mutation_lock = runtime_mutation_lock
    app.state.network_analysis_lock = network_analysis_lock
    app.state.logs = log_buffer

    app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    def safe_detail(exc: object) -> str:
        return log_buffer.redact(str(exc))

    def is_allowed_write_origin(origin: str) -> bool:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        default_port = 443 if parsed.scheme == "https" else 80
        try:
            port = parsed.port or default_port
        except ValueError:
            return False
        allowed_hosts = {settings.app_host, "localhost", "127.0.0.1", "::1"}
        return parsed.hostname in allowed_hosts and port == settings.app_port

    def classify_steam_test_error(exc: object) -> tuple[str, str]:
        message = safe_detail(exc)
        if isinstance(exc, SteamApiError):
            if "缺少 STEAM_API_KEY" in message:
                return "missing_key", "未配置 Steam API Key。请先在左侧“安全配置”里填写并保存 Steam API Key。"
            if exc.status_code in {401, 403}:
                return "invalid_key", f"Steam API Key 无效或无权限（HTTP {exc.status_code}）。请检查 Key 是否复制完整、是否仍可用。"
            if exc.status_code == 429:
                return "rate_limited", "Steam API 暂时限流（HTTP 429）。请稍后再测试，或增加抓取延迟。"
            if exc.status_code is not None:
                return "steam_http", f"Steam API 返回 HTTP {exc.status_code}。请确认 Steam 服务可访问后重试。"
            return "steam_connection", f"无法连接 Steam API。请检查网络或代理设置。详情：{message}"
        return "steam_connection", f"Steam 测试失败。请检查网络或 Steam API Key。详情：{message}"

    def classify_neo4j_test_error(exc: object) -> tuple[str, str]:
        message = safe_detail(exc)
        if not settings.neo4j_password:
            return "missing_password", "未配置 Neo4j 密码。请先在左侧“安全配置”里填写并保存 Neo4j Password。"
        if isinstance(exc, AuthError) or "Unauthorized" in message or "authentication" in message.lower():
            return "auth_failed", "Neo4j 用户名或密码不正确。请检查 NEO4J_USER 和 Neo4j Password。"
        if isinstance(exc, ServiceUnavailable) or "Failed to establish connection" in message or "Connection refused" in message:
            return "server_unavailable", f"无法连接 Neo4j。请确认 Neo4j Desktop/Server 已启动，并且地址是 {settings.neo4j_uri}。"
        return "neo4j_error", f"Neo4j 测试失败。请检查服务、地址和凭据。详情：{message}"

    @app.middleware("http")
    async def csrf_check(request: Request, call_next):  # type: ignore[no-untyped-def]
        """CSRF 防护：仅拦截跨域写请求。同源请求（Origin 为空）放行。"""
        if request.method in ("POST", "PATCH", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and not is_allowed_write_origin(origin):
                return Response(
                    content='{"detail":"Cross-origin request denied"}',
                    status_code=403,
                    media_type="application/json",
                )
        return await call_next(request)

    @app.middleware("http")
    async def log_api_errors(request, call_next):  # type: ignore[no-untyped-def]
        try:
            response = await call_next(request)
        except Exception as exc:
            log_buffer.append("error", "api", f"{request.method} {request.url.path} failed: {exc}")
            raise
        if response.status_code >= 500:
            log_buffer.append("error", "api", f"{request.method} {request.url.path} -> HTTP {response.status_code}")
        elif response.status_code >= 400:
            log_buffer.append("warn", "api", f"{request.method} {request.url.path} -> HTTP {response.status_code}")
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health", response_model=HealthResponse)
    async def health(response: Response) -> HealthResponse:
        try:
            database_message = await call_repository("test_connection")
            health_status = "ok"
        except Exception as exc:
            response.status_code = 503
            database_message = safe_detail(exc)
            health_status = "unavailable"
        return HealthResponse(
            status=health_status,
            database=settings.graph_db_engine,
            database_message=database_message,
            active_crawl=manager.has_active_crawl(),
            project_id=settings.active_project,
        )

    @app.get("/api/settings", response_model=PublicSettings)
    async def get_public_settings() -> PublicSettings:
        async with runtime_mutation_lock:
            return await public_settings()

    @app.get("/api/logs", response_model=list[AppLog])
    async def get_logs(after: Annotated[int, Query(ge=0)] = 0, level: str | None = None) -> list[AppLog]:
        return log_buffer.list(after=after, level=level or None)

    @app.put("/api/settings", response_model=PublicSettings)
    @finish_runtime_mutation("settings")
    async def save_settings(payload: SettingsSave) -> PublicSettings:
        async with runtime_mutation_guard():
            payload_data = payload.model_dump(exclude_none=True)
            env_values = {
                field: value
                for field, value in payload_data.items()
                if field in ENV_KEYS
            }
            secret_values = {
                field: value
                for field, value in payload_data.items()
                if field in {"steam_api_key", "steam_proxy_url", "neo4j_password"}
            }
            previous_env = {field: getattr(settings, field) for field in env_values}
            try:
                previous_secrets = await read_secret_settings(secret_values)
            except SecretStorageError as exc:
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc

            async def rollback() -> list[str]:
                errors = await restore_env_settings(previous_env)
                errors.extend(await restore_secret_settings(previous_secrets))
                if not errors:
                    try:
                        await rebuild_runtime()
                    except Exception as restore_exc:
                        errors.append(f"runtime: {restore_exc}")
                return errors

            try:
                await persist_env_settings(env_values)
                await persist_secret_settings(secret_values)
            except Exception as exc:
                rollback_errors = await rollback()
                log_buffer.append("error", "settings", f"批量配置写入失败: {exc}")
                if rollback_errors:
                    log_buffer.append(
                        "error",
                        "settings",
                        f"批量配置回滚不完整: {'; '.join(rollback_errors)}",
                    )
                status_code = 400 if isinstance(exc, SecretStorageError) else 500
                raise HTTPException(status_code=status_code, detail=safe_detail(exc)) from exc

            try:
                await rebuild_runtime()
            except Exception as exc:
                rollback_errors = await rollback()
                if rollback_errors:
                    log_buffer.append(
                        "error",
                        "settings",
                        f"批量配置回滚不完整: {'; '.join(rollback_errors)}",
                    )
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc

            log_buffer.append("info", "settings", "普通配置与敏感配置已批量保存")
            return await public_settings(
                "配置已保存；如果修改了 APP_HOST 或 APP_PORT，需要重启服务后生效。"
            )

    @app.patch("/api/settings", response_model=PublicSettings)
    @finish_runtime_mutation("settings")
    async def patch_settings(payload: SettingsPatch) -> PublicSettings:
        async with runtime_mutation_guard():
            await asyncio.to_thread(ENV_PATH.touch, exist_ok=True)
            data = payload.model_dump(exclude_none=True)
            previous_values = {field: getattr(settings, field) for field in data}
            try:
                for field, value in data.items():
                    key = ENV_KEYS[field]
                    # 安全：移除换行符防止 .env 注入
                    safe_value = sanitize_env_value(value)
                    await asyncio.to_thread(
                        set_key,
                        str(ENV_PATH),
                        key,
                        safe_value,
                        quote_mode="never",
                    )
            except Exception as exc:
                rollback_errors = await restore_env_settings(previous_values)
                log_buffer.append("error", "settings", f"配置文件写入失败: {exc}")
                if rollback_errors:
                    log_buffer.append(
                        "error",
                        "settings",
                        f"配置文件回滚不完整: {'; '.join(rollback_errors)}",
                    )
                raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc
            try:
                await rebuild_runtime()
            except RuntimeError as exc:
                rollback_errors = await restore_env_settings(previous_values)
                if rollback_errors:
                    log_buffer.append(
                        "error",
                        "settings",
                        f"配置文件回滚不完整: {'; '.join(rollback_errors)}",
                    )
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            message = "配置已保存；如果修改了 APP_HOST 或 APP_PORT，需要重启服务后生效。"
            log_buffer.append("info", "settings", "非敏感配置已保存")
            return await public_settings(message)

    @app.post("/api/settings/secrets", response_model=PublicSettings)
    @finish_runtime_mutation("settings")
    async def set_secret(payload: SecretUpdate) -> PublicSettings:
        async with runtime_mutation_guard():
            try:
                previous_secret = await asyncio.to_thread(secret_store.get, payload.name)
                await asyncio.to_thread(secret_store.set, payload.name, payload.value)
            except SecretStorageError as exc:
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            try:
                await rebuild_runtime()
            except RuntimeError as exc:
                try:
                    if previous_secret:
                        await asyncio.to_thread(secret_store.set, payload.name, previous_secret)
                    else:
                        await asyncio.to_thread(secret_store.delete, payload.name)
                except SecretStorageError as rollback_exc:
                    log_buffer.append(
                        "error",
                        "settings",
                        f"敏感配置回滚失败: {rollback_exc}",
                    )
                finally:
                    clear_settings_cache()
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            log_buffer.append("info", "settings", f"敏感配置已保存: {payload.name}")
            return await public_settings("敏感配置已保存到系统凭据库。")

    @app.delete("/api/settings/secrets/{name}", response_model=PublicSettings)
    @finish_runtime_mutation("settings")
    async def delete_secret(name: str) -> PublicSettings:
        async with runtime_mutation_guard():
            try:
                previous_secret = await asyncio.to_thread(secret_store.get, name)
                await asyncio.to_thread(secret_store.delete, name)
            except SecretStorageError as exc:
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            try:
                await rebuild_runtime()
            except RuntimeError as exc:
                try:
                    if previous_secret:
                        await asyncio.to_thread(secret_store.set, name, previous_secret)
                except SecretStorageError as rollback_exc:
                    log_buffer.append(
                        "error",
                        "settings",
                        f"敏感配置回滚失败: {rollback_exc}",
                    )
                finally:
                    clear_settings_cache()
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            log_buffer.append("warn", "settings", f"敏感配置已删除: {name}")
            return await public_settings("敏感配置已删除。")

    @app.post("/api/settings/test", response_model=SettingsTestResult)
    async def test_settings() -> SettingsTestResult:
        async def operation() -> SettingsTestResult:
            steam_ok = False
            neo4j_ok = False
            steam_reason = "unknown"
            neo4j_reason = "unknown"
            steam_message = "Steam API Key 未测试"
            neo4j_message = "Neo4j 未测试"
            try:
                if provided_steam is not None:
                    await steam.get_player_summaries(["76561197960435530"])
                else:
                    async with SteamClient(
                        settings.steam_api_key,
                        proxy_url=settings.steam_proxy_url,
                    ) as test_steam:
                        await test_steam.get_player_summaries(["76561197960435530"])
                steam_ok = True
                steam_reason = "ok"
                steam_message = "Steam API Key 可用"
            except Exception as exc:
                steam_reason, steam_message = classify_steam_test_error(exc)
                log_buffer.append("warn", "settings", f"Steam 连接测试失败: {steam_message}")
            try:
                current_repo = repo
                await asyncio.to_thread(current_repo.ensure_schema)
                neo4j_message = await asyncio.to_thread(current_repo.test_connection)
                neo4j_ok = True
                neo4j_reason = "ok"
            except Exception as exc:
                neo4j_reason, neo4j_message = classify_neo4j_test_error(exc)
                log_buffer.append("warn", "settings", f"Neo4j 连接测试失败: {neo4j_message}")
            return SettingsTestResult(
                steam_ok=steam_ok,
                neo4j_ok=neo4j_ok,
                steam_message=steam_message,
                neo4j_message=neo4j_message,
                steam_reason=steam_reason,
                neo4j_reason=neo4j_reason,
            )

        return await run_runtime_operation(operation, "settings connection test")

    # ── Project management ────────────────────────────────────────────

    @app.get("/api/projects", response_model=ProjectListResponse)
    async def list_projects() -> ProjectListResponse:
        try:
            result = await call_repository("list_projects")
            result.active_project_id = settings.active_project
            return result
        except Exception as exc:
            log_buffer.append("error", "project", f"Project list read failed: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/projects", response_model=ProjectInfo)
    @finish_runtime_mutation("project")
    async def create_project(payload: ProjectCreate) -> ProjectInfo:
        async with runtime_mutation_guard():
            pid = await asyncio.to_thread(repo.create_project, payload)
        log_buffer.append("info", "project", f"项目已创建: {payload.name} ({pid})")
        return ProjectInfo(id=pid, name=payload.name, created_at=utc_now_iso())

    @app.delete("/api/projects/{project_id}")
    @finish_runtime_mutation("project")
    async def delete_project(project_id: str) -> dict[str, bool]:
        async with runtime_mutation_guard():
            if project_id == "default":
                raise HTTPException(status_code=400, detail="无法删除默认项目")
            was_active = settings.active_project == project_id
            if was_active:
                try:
                    await apply_active_project("default")
                except Exception as exc:
                    rollback_errors = []
                    try:
                        await apply_active_project(project_id)
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                    log_buffer.append(
                        "error",
                        "project",
                        f"Active project switch before deletion failed: {exc}",
                    )
                    if rollback_errors:
                        log_buffer.append(
                            "error",
                            "project",
                            "Active project rollback was incomplete: "
                            + "; ".join(rollback_errors),
                        )
                    raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            try:
                ok = await asyncio.to_thread(repo.delete_project, project_id)
                if not ok:
                    raise HTTPException(status_code=404, detail="项目不存在")
            except Exception as exc:
                rollback_errors = []
                if was_active:
                    try:
                        await apply_active_project(project_id)
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                log_buffer.append("error", "project", f"Project deletion failed: {exc}")
                if rollback_errors:
                    log_buffer.append(
                        "error",
                        "project",
                        "Project deletion rollback was incomplete: "
                        + "; ".join(rollback_errors),
                    )
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc
            log_buffer.append("warn", "project", f"项目已删除: {project_id}")
            return {"ok": True}

    @app.post("/api/projects/switch")
    @finish_runtime_mutation("project")
    async def switch_project(payload: ProjectSwitch) -> ProjectListResponse:
        async with runtime_mutation_guard():
            pid = sanitize_env_value(payload.project_id).strip()
            previous_project_id = settings.active_project
            created_project = False
            if not await asyncio.to_thread(repo.project_exists, pid):
                await asyncio.to_thread(
                    repo.create_project,
                    ProjectCreate(name=pid),
                    project_id=pid,
                )
                created_project = True
                log_buffer.append("info", "project", f"项目已自动创建: {pid}")
            try:
                await apply_active_project(pid)
            except Exception as exc:
                rollback_errors = []
                try:
                    await apply_active_project(previous_project_id)
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
                if created_project:
                    try:
                        await asyncio.to_thread(repo.delete_project, pid)
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                log_buffer.append("error", "project", f"Project switch rolled back: {exc}")
                if rollback_errors:
                    log_buffer.append(
                        "error",
                        "project",
                        f"Project switch rollback was incomplete: {'; '.join(rollback_errors)}",
                    )
                raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
            log_buffer.append("info", "project", f"已切换到项目: {pid}")
            try:
                result = await asyncio.to_thread(repo.list_projects)
                result.active_project_id = pid
                return result
            except Exception as exc:
                log_buffer.append("error", "project", f"Project list read failed: {exc}")
                raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/crawls", response_model=CrawlRun)
    async def create_crawl(payload: CrawlCreate) -> CrawlRun:
        async def operation() -> CrawlRun:
            log_buffer.append("info", "crawl", "正在创建抓取任务")
            return await manager.create_crawl(payload)

        try:
            return await run_runtime_operation(operation, "crawl creation")
        except RuntimeError as exc:
            log_buffer.append("warn", "crawl", f"抓取任务创建冲突: {exc}")
            raise HTTPException(status_code=409, detail=safe_detail(exc)) from exc
        except SteamApiError as exc:
            log_buffer.append("warn", "crawl", f"抓取任务创建失败: {exc}")
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        except Exception as exc:
            log_buffer.append("error", "crawl", f"抓取任务创建异常: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/crawls/active", response_model=CrawlRun | None)
    async def get_active_crawl() -> CrawlRun | None:
        async def operation() -> CrawlRun | None:
            run_id = manager.get_active_run_id()
            if run_id is None:
                return None
            return await asyncio.to_thread(repo.get_crawl_run, run_id)

        return await run_runtime_operation(operation, "active crawl lookup")

    @app.get("/api/crawls/{run_id}", response_model=CrawlRun)
    async def get_crawl(run_id: str) -> CrawlRun:
        run = await call_repository("get_crawl_run", run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Crawl run not found")
        return run

    @app.get("/api/crawls/{run_id}/events", response_model=list[CrawlEvent])
    async def get_crawl_events(run_id: str, after: Annotated[int, Query(ge=0)] = 0) -> list[CrawlEvent]:
        return manager.get_events(run_id, after)

    @app.post("/api/crawls/{run_id}/cancel")
    async def cancel_crawl(run_id: str) -> dict[str, bool]:
        """优雅停止：完成当前层后停止。"""
        return {"cancelled": await manager.cancel(run_id)}

    @app.post("/api/crawls/{run_id}/force-stop")
    async def force_stop_crawl(run_id: str) -> dict[str, bool]:
        """强制中断：立即停止。"""
        return {"stopped": await manager.force_stop(run_id)}

    @app.post("/api/crawls/{run_id}/pause")
    async def pause_crawl(run_id: str) -> dict[str, bool]:
        return {"paused": await manager.pause(run_id)}

    @app.post("/api/crawls/{run_id}/resume")
    async def resume_crawl(run_id: str) -> dict[str, bool]:
        return {"resumed": await manager.resume(run_id)}

    @app.get("/api/graph", response_model=GraphResponse)
    async def get_graph(
        root: str | None = None,
        depth: Annotated[int, Query(ge=0, le=4)] = 2,
        limit: Annotated[int, Query(ge=1, le=100000)] = 500,
        q: str | None = None,
        category: str | None = None,
        friend_count_min: Annotated[int | None, Query(ge=0)] = None,
        friend_count_max: Annotated[int | None, Query(ge=0)] = None,
        prior_pool_min_links: Annotated[int, Query(ge=0)] = 0,
        sort_by: Annotated[str, Query(pattern="^(depth|degree|friend_count|prior_pool_links|closeness)$")] = "depth",
        sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    ) -> GraphResponse:
        try:
            if friend_count_min is not None and friend_count_max is not None and friend_count_min > friend_count_max:
                raise HTTPException(status_code=400, detail="friend_count_min must be <= friend_count_max")
            return await call_repository(
                "get_graph",
                root=root or None,
                depth=depth,
                limit=limit,
                query=q or None,
                category=category or None,
                friend_count_min=friend_count_min,
                friend_count_max=friend_count_max,
                prior_pool_min_links=prior_pool_min_links,
                sort_by=sort_by,
                sort_dir=sort_dir,
                project_scoped=True,
            )
        except HTTPException:
            raise
        except Exception as exc:
            log_buffer.append("error", "graph", f"图谱查询失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/db/stats", response_model=DbStats)
    async def db_stats() -> DbStats:
        try:
            return await call_repository("get_db_stats", project_scoped=True)
        except Exception as exc:
            log_buffer.append("error", "db", f"数据库状态读取失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.patch("/api/users/{steam_id}")
    async def patch_user(steam_id: str, payload: UserPatch) -> dict[str, bool]:
        await call_repository(
            "patch_user",
            steam_id,
            note=payload.note,
            tags=payload.tags,
            category=payload.category,
            project_scoped=True,
        )
        return {"ok": True}

    @app.get("/api/path", response_model=GraphResponse)
    async def get_path(
        from_id: Annotated[str, Query(alias="from")],
        to_id: Annotated[str, Query(alias="to")],
        max_depth: Annotated[int, Query(ge=1, le=4)] = 4,
    ) -> GraphResponse:
        return await call_repository(
            "get_shortest_path",
            from_id,
            to_id,
            max_depth,
            project_scoped=True,
        )

    @app.get("/api/stats/top-degree", response_model=list[GraphNode])
    async def top_degree(limit: Annotated[int, Query(ge=1, le=50)] = 12) -> list[GraphNode]:
        try:
            return await call_repository("get_top_degree", limit, project_scoped=True)
        except Exception as exc:
            log_buffer.append("error", "stats", f"Top degree read failed: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/analysis/friend-circles", response_model=FriendCircleAnalysisResponse)
    async def friend_circles(
        root: str,
        max_depth: Annotated[int, Query(ge=2, le=4)] = 3,
        min_mutual: Annotated[int, Query(ge=0)] = 2,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> FriendCircleAnalysisResponse:
        try:
            return await call_repository(
                "get_friend_circle_analysis",
                root=root,
                max_depth=max_depth,
                min_mutual=min_mutual,
                limit=limit,
                project_scoped=True,
            )
        except Exception as exc:
            log_buffer.append("error", "analysis", f"朋友圈分析失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/analysis/network", response_model=NetworkAnalysisResponse)
    async def network_analysis(
        limit: Annotated[int, Query(ge=1, le=100)] = 12,
        resolution: Annotated[float, Query(gt=0, le=5)] = 1.0,
    ) -> NetworkAnalysisResponse:
        try:
            exported = await call_repository("export_graph", project_scoped=True)
            return await calculate_network_analysis(
                exported,
                limit=limit,
                resolution=resolution,
            )
        except Exception as exc:
            log_buffer.append("error", "analysis", f"网络影响力分析失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/export", response_model=ExportResponse)
    async def export_graph(
        response: Response,
        payload: ExportRequest | None = None,
        format: str | None = None,
    ) -> Response | ExportResponse:
        export_format = payload.format if payload is not None else (format or "json")
        if export_format not in {"json", "csv"}:
            raise HTTPException(status_code=400, detail="format must be json or csv")
        try:
            data = await call_repository("export_graph", project_scoped=True)
        except Exception as exc:
            log_buffer.append("error", "export", f"图谱导出失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc
        if export_format == "json":
            response.headers["Content-Disposition"] = 'attachment; filename="steam_graph.json"'
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return data
        export_project_id = next(
            (str(node["project_id"]) for node in data.nodes if node.get("project_id")),
            settings.active_project,
        )
        return StreamingResponse(
            iter_export_csv(data, export_project_id),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="steam_graph.csv"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
