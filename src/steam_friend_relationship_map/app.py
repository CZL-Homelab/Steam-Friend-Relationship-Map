from __future__ import annotations

import asyncio
import csv
import io
import re
from collections.abc import Iterable
from contextlib import asynccontextmanager
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
from .logs import AppLogBuffer, install_log_handler
from .models import (
    AppLog,
    CrawlCreate,
    CrawlEvent,
    CrawlRun,
    DbStats,
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
    PublicSettings,
    SecretUpdate,
    SettingsPatch,
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


def sanitize_env_value(value: object) -> str:
    return str(value).replace("\n", "").replace("\r", "")


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
    ) -> None:
        self._raise()

    def bulk_patch_users(self, patches: Iterable[dict[str, Any]]) -> None:
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
    settings = settings or get_settings()
    secret_store = secret_store or SecretStore()
    log_buffer = AppLogBuffer()
    log_buffer.set_secret_values(sensitive_setting_values(settings))
    install_log_handler(log_buffer)
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
    steam = steam or SteamClient(settings.steam_api_key, proxy_url=settings.steam_proxy_url)
    manager = CrawlManager(repo, steam, log_buffer, project_id=settings.active_project)

    async def rebuild_runtime() -> None:
        nonlocal settings, repo, steam, manager
        old_settings = settings
        old_repo = repo
        old_steam = steam
        clear_settings_cache()
        settings = get_settings()
        log_buffer.set_secret_values(sensitive_setting_values(settings))
        should_replace_repo = (
            provided_repo is None
            and repository_settings_changed(old_settings, settings)
        )
        if should_replace_repo:
            candidate_repo: IGraphRepository | None = None
            closed_old_repo = False
            try:
                if uses_same_kuzu_database(old_settings, settings) and not isinstance(old_repo, UnavailableRepository):
                    old_repo.close()
                    closed_old_repo = True
                candidate_repo = get_repository(settings)
                candidate_repo.ensure_schema()
            except Exception as exc:
                if candidate_repo is not None:
                    candidate_repo.close()
                if closed_old_repo:
                    try:
                        repo = get_repository(old_settings)
                        repo.ensure_schema()
                    except Exception as restore_exc:
                        log_buffer.append("error", "database", f"Previous graph database restore failed: {restore_exc}")
                        repo = UnavailableRepository(restore_exc)
                else:
                    repo = old_repo
                settings = old_settings
                log_buffer.set_secret_values(sensitive_setting_values(settings))
                manager = CrawlManager(repo, steam, log_buffer, project_id=settings.active_project)
                app.state.repo = repo
                app.state.manager = manager
                log_buffer.append("error", "database", f"Graph database configuration rejected: {exc}")
                raise RuntimeError(f"Graph database configuration was not applied: {exc}") from exc
            if not closed_old_repo:
                old_repo.close()
            repo = candidate_repo
        else:
            repo = old_repo
        try:
            repo.ensure_schema()
        except Exception as exc:
            log_buffer.append("warn", "database", f"数据库 Schema 初始化失败: {exc}")
        steam_settings_changed = (
            old_settings.steam_api_key != settings.steam_api_key
            or old_settings.steam_proxy_url != settings.steam_proxy_url
        )
        if provided_steam is None and steam_settings_changed:
            await old_steam.aclose()
            steam = SteamClient(settings.steam_api_key, proxy_url=settings.steam_proxy_url)
        else:
            steam = old_steam
        manager = CrawlManager(repo, steam, log_buffer, project_id=settings.active_project)
        app.state.repo = repo
        app.state.steam = steam
        app.state.manager = manager

    def public_settings(message: str = "") -> PublicSettings:
        raw = Settings()
        try:
            steam_secret = secret_store.get("steam_api_key")
            proxy_secret = secret_store.get("steam_proxy_url")
            neo4j_secret = secret_store.get("neo4j_password")
            secure_store_available = True
        except SecretStorageError as exc:
            steam_secret = ""
            proxy_secret = ""
            neo4j_secret = ""
            secure_store_available = False
            message = message or str(exc)
        return PublicSettings(
            graph_db_engine=settings.graph_db_engine,
            kuzu_db_path=settings.kuzu_db_path,
            kuzu_buffer_pool_size_gb=settings.kuzu_buffer_pool_size_gb,
            neo4j_uri=settings.neo4j_uri,
            neo4j_user=settings.neo4j_user,
            app_host=settings.app_host,
            app_port=settings.app_port,
            default_max_depth=settings.default_max_depth,
            default_max_nodes=settings.default_max_nodes,
            default_delay_ms=settings.default_delay_ms,
            default_cache_valid_days=settings.default_cache_valid_days,
            active_project=settings.active_project,
            steam_api_key_configured=bool(steam_secret or raw.steam_api_key),
            steam_proxy_configured=bool(proxy_secret or raw.steam_proxy_url),
            neo4j_password_configured=bool(neo4j_secret or raw.neo4j_password),
            steam_api_key_from_env=not bool(steam_secret) and bool(raw.steam_api_key),
            steam_proxy_from_env=not bool(proxy_secret) and bool(raw.steam_proxy_url),
            neo4j_password_from_env=not bool(neo4j_secret) and bool(raw.neo4j_password),
            secure_store_available=secure_store_available,
            message=message,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            try:
                await manager.shutdown()
            finally:
                try:
                    await steam.aclose()
                finally:
                    repo.close()

    app = FastAPI(title="Steam Friend Relationship Map", lifespan=lifespan)
    app.state.repo = repo
    app.state.steam = steam
    app.state.manager = manager
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
            database_message = repo.test_connection()
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
        return public_settings()

    @app.get("/api/logs", response_model=list[AppLog])
    async def get_logs(after: Annotated[int, Query(ge=0)] = 0, level: str | None = None) -> list[AppLog]:
        return log_buffer.list(after=after, level=level or None)

    @app.patch("/api/settings", response_model=PublicSettings)
    async def patch_settings(payload: SettingsPatch) -> PublicSettings:
        if manager.has_active_crawl():
            raise HTTPException(status_code=400, detail="当前有活跃的抓取任务在运行，请先停止任务后再修改配置。")
        ENV_PATH.touch(exist_ok=True)
        data = payload.model_dump(exclude_none=True)
        previous_values = {field: getattr(settings, field) for field in data}
        for field, value in data.items():
            key = ENV_KEYS[field]
            # 安全：移除换行符防止 .env 注入
            safe_value = sanitize_env_value(value)
            set_key(str(ENV_PATH), key, safe_value, quote_mode="never")
        try:
            await rebuild_runtime()
        except RuntimeError as exc:
            for field, value in previous_values.items():
                set_key(
                    str(ENV_PATH),
                    ENV_KEYS[field],
                    sanitize_env_value(value),
                    quote_mode="never",
                )
            clear_settings_cache()
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        message = "配置已保存；如果修改了 APP_HOST 或 APP_PORT，需要重启服务后生效。"
        log_buffer.append("info", "settings", "非敏感配置已保存")
        return public_settings(message)

    @app.post("/api/settings/secrets", response_model=PublicSettings)
    async def set_secret(payload: SecretUpdate) -> PublicSettings:
        if manager.has_active_crawl():
            raise HTTPException(status_code=400, detail="当前有活跃的抓取任务在运行，请先停止任务后再修改配置。")
        try:
            previous_secret = secret_store.get(payload.name)
            secret_store.set(payload.name, payload.value)
        except SecretStorageError as exc:
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        try:
            await rebuild_runtime()
        except RuntimeError as exc:
            try:
                if previous_secret:
                    secret_store.set(payload.name, previous_secret)
                else:
                    secret_store.delete(payload.name)
            finally:
                clear_settings_cache()
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        log_buffer.append("info", "settings", f"敏感配置已保存: {payload.name}")
        return public_settings("敏感配置已保存到系统凭据库。")

    @app.delete("/api/settings/secrets/{name}", response_model=PublicSettings)
    async def delete_secret(name: str) -> PublicSettings:
        if manager.has_active_crawl():
            raise HTTPException(status_code=400, detail="当前有活跃的抓取任务在运行，请先停止任务后再修改配置。")
        try:
            previous_secret = secret_store.get(name)
            secret_store.delete(name)
        except SecretStorageError as exc:
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        try:
            await rebuild_runtime()
        except RuntimeError as exc:
            try:
                if previous_secret:
                    secret_store.set(name, previous_secret)
            finally:
                clear_settings_cache()
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        log_buffer.append("warn", "settings", f"敏感配置已删除: {name}")
        return public_settings("敏感配置已删除。")

    @app.post("/api/settings/test", response_model=SettingsTestResult)
    async def test_settings() -> SettingsTestResult:
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
            repo.ensure_schema()
            neo4j_message = repo.test_connection()
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

    # ── Project management ────────────────────────────────────────────

    @app.get("/api/projects", response_model=ProjectListResponse)
    async def list_projects() -> ProjectListResponse:
        try:
            result = repo.list_projects()
            result.active_project_id = settings.active_project
            return result
        except Exception as exc:
            log_buffer.append("error", "project", f"Project list read failed: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/projects", response_model=ProjectInfo)
    async def create_project(payload: ProjectCreate) -> ProjectInfo:
        pid = repo.create_project(payload)
        log_buffer.append("info", "project", f"项目已创建: {payload.name} ({pid})")
        return ProjectInfo(id=pid, name=payload.name, created_at=utc_now_iso())

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str) -> dict[str, bool]:
        if manager.has_active_crawl():
            raise HTTPException(status_code=400, detail="当前有活跃的抓取任务在运行，请先停止任务后再删除项目。")
        if project_id == "default":
            raise HTTPException(status_code=400, detail="无法删除默认项目")
        ok = repo.delete_project(project_id)
        if not ok:
            raise HTTPException(status_code=404, detail="项目不存在")
        log_buffer.append("warn", "project", f"项目已删除: {project_id}")
        # 如果删除的是当前活动项目，切回 default
        if settings.active_project == project_id:
            set_key(str(ENV_PATH), "ACTIVE_PROJECT", "default", quote_mode="never")
            await rebuild_runtime()
        return {"ok": True}

    @app.post("/api/projects/switch")
    async def switch_project(payload: ProjectCreate) -> ProjectListResponse:
        if manager.has_active_crawl():
            raise HTTPException(status_code=400, detail="当前有活跃的抓取任务在运行，请先停止任务后再切换项目。")
        """Switch active project. payload.name = project_id"""
        pid = sanitize_env_value(payload.name).strip()
        if not pid:
            raise HTTPException(status_code=400, detail="项目 ID 不能为空")
        # Ensure the project exists
        if not repo.project_exists(pid):
            # Auto-create if not exists
            repo.create_project(ProjectCreate(name=pid), project_id=pid)
            log_buffer.append("info", "project", f"项目已自动创建: {pid}")
        ENV_PATH.touch(exist_ok=True)
        set_key(str(ENV_PATH), "ACTIVE_PROJECT", sanitize_env_value(pid), quote_mode="never")
        await rebuild_runtime()
        log_buffer.append("info", "project", f"已切换到项目: {pid}")
        try:
            result = repo.list_projects()
            result.active_project_id = pid
            return result
        except Exception as exc:
            log_buffer.append("error", "project", f"Project list read failed: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/crawls", response_model=CrawlRun)
    async def create_crawl(payload: CrawlCreate) -> CrawlRun:
        try:
            log_buffer.append("info", "crawl", "正在创建抓取任务")
            return await manager.create_crawl(payload)
        except RuntimeError as exc:
            log_buffer.append("warn", "crawl", f"抓取任务创建冲突: {exc}")
            raise HTTPException(status_code=409, detail=safe_detail(exc)) from exc
        except SteamApiError as exc:
            log_buffer.append("warn", "crawl", f"抓取任务创建失败: {exc}")
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        except Exception as exc:
            log_buffer.append("error", "crawl", f"抓取任务创建异常: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/crawls/{run_id}", response_model=CrawlRun)
    async def get_crawl(run_id: str) -> CrawlRun:
        run = repo.get_crawl_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Crawl run not found")
        return run

    @app.get("/api/crawls/{run_id}/events", response_model=list[CrawlEvent])
    async def get_crawl_events(run_id: str, after: Annotated[int, Query(ge=0)] = 0) -> list[CrawlEvent]:
        return manager.get_events(run_id, after)

    @app.post("/api/crawls/{run_id}/cancel")
    async def cancel_crawl(run_id: str) -> dict[str, bool]:
        """优雅停止：完成当前层后停止。"""
        return {"cancelled": manager.cancel(run_id)}

    @app.post("/api/crawls/{run_id}/force-stop")
    async def force_stop_crawl(run_id: str) -> dict[str, bool]:
        """强制中断：立即停止。"""
        return {"stopped": manager.force_stop(run_id)}

    @app.post("/api/crawls/{run_id}/pause")
    async def pause_crawl(run_id: str) -> dict[str, bool]:
        return {"paused": manager.pause(run_id)}

    @app.post("/api/crawls/{run_id}/resume")
    async def resume_crawl(run_id: str) -> dict[str, bool]:
        return {"resumed": manager.resume(run_id)}

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
            return repo.get_graph(
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
                project_id=settings.active_project,
            )
        except HTTPException:
            raise
        except Exception as exc:
            log_buffer.append("error", "graph", f"图谱查询失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/db/stats", response_model=DbStats)
    async def db_stats() -> DbStats:
        try:
            return repo.get_db_stats(project_id=settings.active_project)
        except Exception as exc:
            log_buffer.append("error", "db", f"数据库状态读取失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.patch("/api/users/{steam_id}")
    async def patch_user(steam_id: str, payload: UserPatch) -> dict[str, bool]:
        repo.patch_user(steam_id, note=payload.note, tags=payload.tags, category=payload.category)
        return {"ok": True}

    @app.get("/api/path", response_model=GraphResponse)
    async def get_path(
        from_id: Annotated[str, Query(alias="from")],
        to_id: Annotated[str, Query(alias="to")],
        max_depth: Annotated[int, Query(ge=1, le=4)] = 4,
    ) -> GraphResponse:
        return repo.get_shortest_path(from_id, to_id, max_depth, project_id=settings.active_project)

    @app.get("/api/stats/top-degree", response_model=list[GraphNode])
    async def top_degree(limit: Annotated[int, Query(ge=1, le=50)] = 12) -> list[GraphNode]:
        try:
            return repo.get_top_degree(limit, project_id=settings.active_project)
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
            return repo.get_friend_circle_analysis(root=root, max_depth=max_depth, min_mutual=min_mutual, limit=limit, project_id=settings.active_project)
        except Exception as exc:
            log_buffer.append("error", "analysis", f"朋友圈分析失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/analysis/network", response_model=NetworkAnalysisResponse)
    async def network_analysis(
        limit: Annotated[int, Query(ge=1, le=100)] = 12,
        resolution: Annotated[float, Query(gt=0, le=5)] = 1.0,
    ) -> NetworkAnalysisResponse:
        try:
            exported = repo.export_graph(project_id=settings.active_project)
            return await asyncio.to_thread(
                analyze_network,
                exported,
                limit=limit,
                resolution=resolution,
            )
        except Exception as exc:
            log_buffer.append("error", "analysis", f"网络影响力分析失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/export", response_model=ExportResponse)
    async def export_graph(format: str = "json") -> Response | ExportResponse:
        data = repo.export_graph(project_id=settings.active_project)
        if format == "json":
            return data
        if format != "csv":
            raise HTTPException(status_code=400, detail="format must be json or csv")
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["type", "id", "label", "source", "target", "profile_url", "note", "category"])
        writer.writeheader()
        for node in data.nodes:
            writer.writerow(
                {
                    "type": "node",
                    "id": node.get("steam_id", ""),
                    "label": node.get("persona_name", ""),
                    "source": "",
                    "target": "",
                    "profile_url": node.get("profile_url", ""),
                    "note": node.get("note", ""),
                    "category": node.get("category", ""),
                }
            )
        for edge in data.edges:
            writer.writerow({"type": "edge", "id": "", "label": "", "source": edge["source"], "target": edge["target"], "profile_url": "", "note": "", "category": ""})
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=steam_graph.csv"})

    return app
