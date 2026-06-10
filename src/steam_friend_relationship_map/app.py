from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .crawler import CrawlManager
from .models import CrawlCreate, CrawlRun, ExportResponse, GraphNode, GraphResponse, SettingsTestResult, UserPatch
from .neo4j_repo import Neo4jRepository
from .settings import Settings, get_settings
from .steam import SteamApiError, SteamClient


STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    repo: Neo4jRepository | None = None,
    steam: SteamClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    repo = repo or Neo4jRepository(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    steam = steam or SteamClient(settings.steam_api_key)
    manager = CrawlManager(repo, steam)

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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

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
        try:
            repo.ensure_schema()
            neo4j_message = repo.test_connection()
            neo4j_ok = True
        except Exception as exc:
            neo4j_message = str(exc)
        return SettingsTestResult(steam_ok=steam_ok, neo4j_ok=neo4j_ok, steam_message=steam_message, neo4j_message=neo4j_message)

    @app.post("/api/crawls", response_model=CrawlRun)
    async def create_crawl(payload: CrawlCreate) -> CrawlRun:
        try:
            return await manager.create_crawl(payload)
        except SteamApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/crawls/{run_id}", response_model=CrawlRun)
    async def get_crawl(run_id: str) -> CrawlRun:
        run = repo.get_crawl_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Crawl run not found")
        return run

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
    ) -> GraphResponse:
        try:
            return repo.get_graph(root=root or None, depth=depth, limit=limit, query=q or None, category=category or None)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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

    @app.get("/api/export", response_model=ExportResponse)
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
