from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import set_key
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .crawler import CrawlManager
from .logs import AppLogBuffer, install_log_handler
from .models import (
    AppLog,
    CrawlCreate,
    CrawlEvent,
    CrawlRun,
    DbStats,
    ExportResponse,
    FriendCircleAnalysisResponse,
    GraphNode,
    GraphResponse,
    PublicSettings,
    SecretUpdate,
    SettingsPatch,
    SettingsTestResult,
    UserPatch,
)
from .neo4j_repo import Neo4jRepository
from .secrets import SecretStorageError, SecretStore
from .settings import Settings, clear_settings_cache, get_settings
from .steam import SteamApiError, SteamClient


STATIC_DIR = Path(__file__).parent / "static"
ENV_PATH = Path.cwd() / ".env"
ENV_KEYS = {
    "neo4j_uri": "NEO4J_URI",
    "neo4j_user": "NEO4J_USER",
    "app_host": "APP_HOST",
    "app_port": "APP_PORT",
    "default_max_depth": "DEFAULT_MAX_DEPTH",
    "default_max_nodes": "DEFAULT_MAX_NODES",
    "default_delay_ms": "DEFAULT_DELAY_MS",
}


def create_app(
    settings: Settings | None = None,
    repo: Neo4jRepository | None = None,
    steam: SteamClient | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    provided_repo = repo
    provided_steam = steam
    settings = settings or get_settings()
    secret_store = secret_store or SecretStore()
    log_buffer = AppLogBuffer()
    log_buffer.set_secret_values([settings.steam_api_key, settings.neo4j_password])
    install_log_handler(log_buffer)
    repo = repo or Neo4jRepository(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    steam = steam or SteamClient(settings.steam_api_key)
    manager = CrawlManager(repo, steam, log_buffer)

    async def rebuild_runtime() -> None:
        nonlocal settings, repo, steam, manager
        old_repo = repo
        old_steam = steam
        clear_settings_cache()
        settings = get_settings()
        log_buffer.set_secret_values([settings.steam_api_key, settings.neo4j_password])
        repo = old_repo if provided_repo is not None else Neo4jRepository(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        steam = old_steam if provided_steam is not None else SteamClient(settings.steam_api_key)
        manager = CrawlManager(repo, steam, log_buffer)
        app.state.repo = repo
        app.state.steam = steam
        app.state.manager = manager
        if provided_repo is None:
            old_repo.close()
        if provided_steam is None:
            await old_steam.aclose()

    def public_settings(message: str = "") -> PublicSettings:
        raw = Settings()
        try:
            steam_secret = secret_store.get("steam_api_key")
            neo4j_secret = secret_store.get("neo4j_password")
            secure_store_available = True
        except SecretStorageError as exc:
            steam_secret = ""
            neo4j_secret = ""
            secure_store_available = False
            message = message or str(exc)
        return PublicSettings(
            neo4j_uri=settings.neo4j_uri,
            neo4j_user=settings.neo4j_user,
            app_host=settings.app_host,
            app_port=settings.app_port,
            default_max_depth=settings.default_max_depth,
            default_max_nodes=settings.default_max_nodes,
            default_delay_ms=settings.default_delay_ms,
            steam_api_key_configured=bool(steam_secret or raw.steam_api_key),
            neo4j_password_configured=bool(neo4j_secret or raw.neo4j_password),
            steam_api_key_from_env=not bool(steam_secret) and bool(raw.steam_api_key),
            neo4j_password_from_env=not bool(neo4j_secret) and bool(raw.neo4j_password),
            secure_store_available=secure_store_available,
            message=message,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await steam.aclose()
            repo.close()

    app = FastAPI(title="Steam Friend Relationship Map", lifespan=lifespan)
    app.state.repo = repo
    app.state.steam = steam
    app.state.manager = manager
    app.state.logs = log_buffer

    app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    def safe_detail(exc: object) -> str:
        return log_buffer.redact(str(exc))

    @app.middleware("http")
    async def csrf_check(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in ("POST", "PATCH", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin:
                host = f"http://{settings.app_host}:{settings.app_port}"
                localhost = f"http://localhost:{settings.app_port}"
                if not (origin.startswith(host) or origin.startswith(localhost)):
                    return Response(content='{"detail":"Cross-origin request denied"}', status_code=403, media_type="application/json")
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

    @app.get("/api/settings", response_model=PublicSettings)
    async def get_public_settings() -> PublicSettings:
        return public_settings()

    @app.get("/api/logs", response_model=list[AppLog])
    async def get_logs(after: Annotated[int, Query(ge=0)] = 0, level: str | None = None) -> list[AppLog]:
        return log_buffer.list(after=after, level=level or None)

    @app.patch("/api/settings", response_model=PublicSettings)
    async def patch_settings(payload: SettingsPatch) -> PublicSettings:
        ENV_PATH.touch(exist_ok=True)
        data = payload.model_dump(exclude_none=True)
        for field, value in data.items():
            key = ENV_KEYS[field]
            set_key(str(ENV_PATH), key, str(value), quote_mode="never")
        await rebuild_runtime()
        message = "配置已保存；如果修改了 APP_HOST 或 APP_PORT，需要重启服务后生效。"
        log_buffer.append("info", "settings", "非敏感配置已保存")
        return public_settings(message)

    @app.post("/api/settings/secrets", response_model=PublicSettings)
    async def set_secret(payload: SecretUpdate) -> PublicSettings:
        try:
            secret_store.set(payload.name, payload.value)
        except SecretStorageError as exc:
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        await rebuild_runtime()
        log_buffer.append("info", "settings", f"敏感配置已保存: {payload.name}")
        return public_settings("敏感配置已保存到系统凭据库。")

    @app.delete("/api/settings/secrets/{name}", response_model=PublicSettings)
    async def delete_secret(name: str) -> PublicSettings:
        try:
            secret_store.delete(name)
        except SecretStorageError as exc:
            raise HTTPException(status_code=400, detail=safe_detail(exc)) from exc
        await rebuild_runtime()
        log_buffer.append("warn", "settings", f"敏感配置已删除: {name}")
        return public_settings("敏感配置已删除。")

    @app.post("/api/settings/test", response_model=SettingsTestResult)
    async def test_settings() -> SettingsTestResult:
        steam_ok = False
        neo4j_ok = False
        steam_message = "Steam API Key 未测试"
        neo4j_message = "Neo4j 未测试"
        try:
            async with SteamClient(settings.steam_api_key) as test_steam:
                await test_steam.get_player_summaries(["76561197960435530"])
            steam_ok = True
            steam_message = "Steam API Key 可用"
        except Exception as exc:
            steam_message = str(exc)
            log_buffer.append("warn", "settings", f"Steam 连接测试失败: {exc}")
        try:
            repo.ensure_schema()
            neo4j_message = repo.test_connection()
            neo4j_ok = True
        except Exception as exc:
            neo4j_message = str(exc)
            log_buffer.append("warn", "settings", f"Neo4j 连接测试失败: {exc}")
        return SettingsTestResult(steam_ok=steam_ok, neo4j_ok=neo4j_ok, steam_message=steam_message, neo4j_message=neo4j_message)

    @app.post("/api/crawls", response_model=CrawlRun)
    async def create_crawl(payload: CrawlCreate) -> CrawlRun:
        try:
            log_buffer.append("info", "crawl", "正在创建抓取任务")
            return await manager.create_crawl(payload)
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
        return {"cancelled": manager.cancel(run_id)}

    @app.get("/api/graph", response_model=GraphResponse)
    async def get_graph(
        root: str | None = None,
        depth: Annotated[int, Query(ge=0, le=4)] = 2,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
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
            )
        except HTTPException:
            raise
        except Exception as exc:
            log_buffer.append("error", "graph", f"图谱查询失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.get("/api/db/stats", response_model=DbStats)
    async def db_stats() -> DbStats:
        try:
            return repo.get_db_stats()
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
        return repo.get_shortest_path(from_id, to_id, max_depth)

    @app.get("/api/stats/top-degree", response_model=list[GraphNode])
    async def top_degree(limit: Annotated[int, Query(ge=1, le=50)] = 12) -> list[GraphNode]:
        return repo.get_top_degree(limit)

    @app.get("/api/analysis/friend-circles", response_model=FriendCircleAnalysisResponse)
    async def friend_circles(
        root: str,
        max_depth: Annotated[int, Query(ge=2, le=4)] = 3,
        min_mutual: Annotated[int, Query(ge=0)] = 2,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> FriendCircleAnalysisResponse:
        try:
            return repo.get_friend_circle_analysis(root=root, max_depth=max_depth, min_mutual=min_mutual, limit=limit)
        except Exception as exc:
            log_buffer.append("error", "analysis", f"朋友圈分析失败: {exc}")
            raise HTTPException(status_code=500, detail=safe_detail(exc)) from exc

    @app.post("/api/export", response_model=ExportResponse)
    async def export_graph(format: str = "json") -> Response | ExportResponse:
        data = repo.export_graph()
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
