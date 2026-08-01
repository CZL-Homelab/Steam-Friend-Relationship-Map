from __future__ import annotations

import asyncio
import csv
import io
import logging
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from steam_friend_relationship_map.app import create_app, iter_export_csv
from steam_friend_relationship_map.kuzu_repo import KuzuRepositoryImpl
from steam_friend_relationship_map.logs import AppLogHandler
from steam_friend_relationship_map.models import (
    CrawlRun,
    CrawlStatus,
    DbStats,
    ExportResponse,
    FriendCircleAnalysisResponse,
    FriendCircleCandidate,
    GraphEdge,
    GraphNode,
    GraphResponse,
    NetworkAnalysisResponse,
)
from steam_friend_relationship_map.settings import Settings
from steam_friend_relationship_map.secrets import SecretStorageError
from steam_friend_relationship_map.steam import SteamApiError, SteamClient


class FakeRepo:
    def close(self) -> None:
        pass

    def ensure_schema(self) -> None:
        pass

    def test_connection(self) -> str:
        return "Neo4j 连接正常"

    def ensure_default_project(self) -> str:
        return "default"

    def list_projects(self) -> object:
        from steam_friend_relationship_map.models import ProjectInfo, ProjectListResponse
        return ProjectListResponse(
            projects=[ProjectInfo(id="default", name="默认项目")],
            active_project_id="default",
        )

    def create_project(self, payload: object, project_id: str | None = None) -> str:
        return project_id or "default"

    def delete_project(self, project_id: str) -> bool:
        return project_id != "default"

    def project_exists(self, project_id: str) -> bool:
        return True

    def get_graph(self, **_: object) -> GraphResponse:
        return GraphResponse(
            nodes=[
                GraphNode(
                    id="root",
                    label="Root",
                    degree=1,
                    root_route_count=1,
                    root_route_total_hops=0,
                    root_friend_circle_score=1_000_000,
                )
            ],
            edges=[GraphEdge(id="root-a", source="root", target="a")],
            requested_depth=2,
            traversal_depth_reached=1,
            root_found=True,
            depth_incomplete=True,
        )

    def patch_user(self, *_: object, **__: object) -> None:
        pass

    def get_shortest_path(self, *_: object, **__: object) -> GraphResponse:
        return GraphResponse(nodes=[GraphNode(id="root", label="Root")], edges=[])

    def get_top_degree(self, limit: int = 12, project_id: str = "default") -> list[GraphNode]:
        return [GraphNode(id="root", label="Root", degree=5)]

    def get_db_stats(self, project_id: str = "default") -> DbStats:
        return DbStats(steam_users=2, steam_friend_relationships=1, crawl_runs=1)

    def get_friend_circle_analysis(self, **_: object) -> FriendCircleAnalysisResponse:
        return FriendCircleAnalysisResponse(
            root="root",
            candidates=[FriendCircleCandidate(steam_id="candidate", label="Candidate", mutual_count=2, score=18)],
        )

    def export_graph(self, project_id: str = "default") -> ExportResponse:
        return ExportResponse(nodes=[{"steam_id": "root", "persona_name": "Root"}], edges=[])

    def get_crawl_run(self, _: str) -> None:
        return None


class FakeSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str:
        return self.values.get(name, "")

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class FakeSteam:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc

    async def aclose(self) -> None:
        pass

    async def resolve_steam_id(self, value: str) -> str:
        return value

    async def get_player_summaries(self, steam_ids: list[str]) -> list[object]:
        if self.exc:
            raise self.exc
        return []

    async def get_friend_list(self, steam_id: str) -> object:
        raise NotImplementedError


class FailingRepo(FakeRepo):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def ensure_schema(self) -> None:
        raise self.exc


class TrackingRepo(FakeRepo):
    def __init__(self, schema_error: Exception | None = None) -> None:
        self.schema_error = schema_error
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1

    def ensure_schema(self) -> None:
        if self.schema_error:
            raise self.schema_error


class RecoveringRepo(FakeRepo):
    def __init__(self, interrupted: int) -> None:
        self.interrupted = interrupted
        self.recovery_calls = 0

    def recover_interrupted_crawls(self) -> int:
        self.recovery_calls += 1
        return self.interrupted


def test_app_recovers_interrupted_crawls_on_startup() -> None:
    repo = RecoveringRepo(interrupted=2)
    app = create_app(
        settings=Settings(),
        repo=repo,  # type: ignore[arg-type]
        steam=SteamClient("key"),
        secret_store=FakeSecretStore(),
    )
    client = TestClient(app)

    assert repo.recovery_calls == 1
    logs = client.get("/api/logs?after=0").json()
    assert any(
        entry["source"] == "crawl:recovery"
        and "2 个上次启动中断" in entry["message"]
        for entry in logs
    )


def test_graph_endpoint_uses_repo() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/graph?root=root&depth=2&limit=50")

    assert response.status_code == 200
    body = response.json()
    assert body["nodes"][0]["id"] == "root"
    assert body["requested_depth"] == 2
    assert body["traversal_depth_reached"] == 1
    assert body["root_found"] is True
    assert body["depth_incomplete"] is True
    assert body["nodes"][0]["root_route_count"] == 1
    assert body["nodes"][0]["root_route_total_hops"] == 0
    assert body["nodes"][0]["root_friend_circle_score"] == 1_000_000


def test_project_list_errors_return_json_detail() -> None:
    class BrokenProjectsRepo(FakeRepo):
        def list_projects(self) -> object:
            raise RuntimeError("buffer pool is full")

    app = create_app(settings=Settings(), repo=BrokenProjectsRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/projects")

    assert response.status_code == 500
    assert response.json()["detail"] == "buffer pool is full"


def test_app_starts_when_repository_is_unavailable() -> None:
    with patch("steam_friend_relationship_map.app.get_repository", side_effect=RuntimeError("Could not set lock on file")):
        app = create_app(settings=Settings(), steam=SteamClient("key"), secret_store=FakeSecretStore())

    client = TestClient(app)

    settings_response = client.get("/api/settings")
    projects_response = client.get("/api/projects")

    assert settings_response.status_code == 200
    assert projects_response.status_code == 500
    assert "Graph database is unavailable" in projects_response.json()["detail"]


def test_user_patch_endpoint() -> None:
    class PatchTrackingRepo(FakeRepo):
        def __init__(self) -> None:
            self.project_id = ""

        def patch_user(self, *_: object, **kwargs: object) -> None:
            self.project_id = str(kwargs["project_id"])

    repo = PatchTrackingRepo()
    app = create_app(settings=Settings(active_project="project-a"), repo=repo, steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.patch("/api/users/root", json={"note": "friend", "tags": ["cs2", "cs2"], "category": "game"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert repo.project_id == "project-a"


def test_db_stats_endpoint() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/db/stats")

    assert response.status_code == 200
    assert response.json()["steam_users"] == 2
    assert response.json()["steam_friend_relationships"] == 1


def test_secret_api_does_not_echo_secret() -> None:
    store = FakeSecretStore()
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/settings/secrets", json={"name": "steam_api_key", "value": "super-secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["steam_api_key_configured"] is True
    assert "super-secret" not in response.text


def test_batch_settings_save_rebuilds_runtime_once_and_never_echoes_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    class CountingSchemaRepo(FakeRepo):
        def __init__(self) -> None:
            self.schema_calls = 0

        def ensure_schema(self) -> None:
            self.schema_calls += 1

    old_settings = Settings(default_max_depth=2)
    new_settings = old_settings.model_copy(
        update={
            "default_max_depth": 3,
            "steam_api_key": "new-steam-key",
            "steam_proxy_url": "socks5://127.0.0.1:1080",
            "neo4j_password": "new-neo4j-password",
        }
    )
    settings_loader = MagicMock(return_value=new_settings)
    env_writes: list[tuple[str, str]] = []

    def record_set_key(_path: str, key: str, value: str, **_: object) -> None:
        env_writes.append((key, value))

    repo = CountingSchemaRepo()
    store = FakeSecretStore()
    monkeypatch.setattr(app_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app_module, "set_key", record_set_key)
    monkeypatch.setattr(app_module, "get_settings", settings_loader)
    app = create_app(
        settings=old_settings,
        repo=repo,
        steam=FakeSteam(),
        secret_store=store,
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.put(
        "/api/settings",
        json={
            "default_max_depth": 3,
            "steam_api_key": "new-steam-key",
            "steam_proxy_url": "socks5://127.0.0.1:1080",
            "neo4j_password": "new-neo4j-password",
        },
    )

    assert response.status_code == 200
    assert env_writes == [("DEFAULT_MAX_DEPTH", "3")]
    assert store.values == {
        "steam_api_key": "new-steam-key",
        "steam_proxy_url": "socks5://127.0.0.1:1080",
        "neo4j_password": "new-neo4j-password",
    }
    assert settings_loader.call_count == 1
    assert repo.schema_calls == 2  # startup plus one combined runtime rebuild
    assert "new-steam-key" not in response.text
    assert "new-neo4j-password" not in response.text


def test_batch_settings_save_rolls_back_env_and_partial_secret_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    class PartiallyFailingSecretStore(FakeSecretStore):
        def set(self, name: str, value: str) -> None:
            if name == "neo4j_password" and value == "bad-password":
                raise SecretStorageError("credential write failed")
            super().set(name, value)

    env_values = {"DEFAULT_MAX_DEPTH": "2"}

    def write_env(_path: str, key: str, value: str, **_: object) -> None:
        env_values[key] = value

    def load_settings() -> Settings:
        return Settings(default_max_depth=int(env_values["DEFAULT_MAX_DEPTH"]))

    store = PartiallyFailingSecretStore()
    store.values = {
        "steam_api_key": "old-steam-key",
        "neo4j_password": "old-password",
    }
    monkeypatch.setattr(app_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app_module, "set_key", write_env)
    monkeypatch.setattr(app_module, "get_settings", load_settings)
    app = create_app(
        settings=Settings(default_max_depth=2),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=store,
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.put(
        "/api/settings",
        json={
            "default_max_depth": 3,
            "steam_api_key": "new-steam-key",
            "neo4j_password": "bad-password",
        },
    )

    assert response.status_code == 400
    assert "credential write failed" in response.json()["detail"]
    assert env_values == {"DEFAULT_MAX_DEPTH": "2"}
    assert store.values == {
        "steam_api_key": "old-steam-key",
        "neo4j_password": "old-password",
    }
    assert client.get("/api/settings").json()["default_max_depth"] == 2


def test_batch_settings_save_rolls_back_all_values_when_database_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    old_settings = Settings(kuzu_db_path="data/current", steam_api_key="old-key")
    new_settings = old_settings.model_copy(
        update={"kuzu_db_path": "data/missing", "steam_api_key": "new-key"}
    )
    old_repo = TrackingRepo()
    candidate_repo = TrackingRepo(RuntimeError("unable to open database"))
    store = FakeSecretStore()
    store.values["steam_api_key"] = "old-key"
    settings_loader = MagicMock(side_effect=[new_settings, old_settings])
    env_writes: list[tuple[str, str]] = []

    def record_set_key(_path: str, key: str, value: str, **_: object) -> None:
        env_writes.append((key, value))

    monkeypatch.setattr(app_module, "set_key", record_set_key)
    monkeypatch.setattr(app_module, "get_settings", settings_loader)
    monkeypatch.setattr(
        app_module,
        "get_repository",
        MagicMock(side_effect=[old_repo, candidate_repo]),
    )
    app = create_app(
        settings=old_settings,
        steam=FakeSteam(),
        secret_store=store,
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.put(
        "/api/settings",
        json={"kuzu_db_path": "data/missing", "steam_api_key": "new-key"},
    )

    assert response.status_code == 400
    assert app.state.repo is old_repo
    assert candidate_repo.close_count == 1
    assert store.values["steam_api_key"] == "old-key"
    assert env_writes == [
        ("KUZU_DB_PATH", "data/missing"),
        ("KUZU_DB_PATH", "data/current"),
    ]
    assert settings_loader.call_count == 2


def test_public_settings_reports_explicit_runtime_secrets_without_echoing_them() -> None:
    api_key = "runtime-steam-key"
    password = "runtime-neo4j-password"
    app = create_app(
        settings=Settings(steam_api_key=api_key, neo4j_password=password),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["steam_api_key_configured"] is True
    assert body["neo4j_password_configured"] is True
    assert api_key not in response.text
    assert password not in response.text


def test_secret_api_rejects_unknown_secret_name() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/settings/secrets", json={"name": "cookie", "value": "secret"})

    assert response.status_code == 422


def test_proxy_secret_status_does_not_echo_url() -> None:
    proxy_url = "http://proxy-user:proxy-password@127.0.0.1:8080"
    store = FakeSecretStore()
    store.values["steam_proxy_url"] = proxy_url
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=FakeSteam(), secret_store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["steam_proxy_configured"] is True
    assert response.json()["steam_proxy_from_env"] is False
    assert proxy_url not in response.text
    assert "proxy-password" not in response.text


def test_proxy_secret_rejects_unsupported_scheme() -> None:
    store = FakeSecretStore()
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=FakeSteam(), secret_store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post(
        "/api/settings/secrets",
        json={"name": "steam_proxy_url", "value": "ftp://127.0.0.1:21"},
    )

    assert response.status_code == 422
    assert "steam_proxy_url" not in store.values


def test_proxy_secret_update_rebuilds_steam_client() -> None:
    proxy_url = "socks5://127.0.0.1:1080"
    old_settings = Settings(steam_api_key="key")
    new_settings = old_settings.model_copy(update={"steam_proxy_url": proxy_url})
    old_steam = MagicMock()
    old_steam.aclose = AsyncMock()
    new_steam = MagicMock()
    store = FakeSecretStore()

    with (
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
        patch("steam_friend_relationship_map.app.SteamClient", side_effect=[old_steam, new_steam]) as steam_client,
    ):
        app = create_app(settings=old_settings, repo=FakeRepo(), secret_store=store)  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.post(
            "/api/settings/secrets",
            json={"name": "steam_proxy_url", "value": proxy_url},
        )

    assert response.status_code == 200
    assert app.state.steam is new_steam
    old_steam.aclose.assert_awaited_once()
    assert steam_client.call_args_list[0].kwargs == {"proxy_url": ""}
    assert steam_client.call_args_list[1].kwargs == {"proxy_url": proxy_url}


def test_settings_patch_rejects_invalid_graph_engine() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.patch("/api/settings", json={"graph_db_engine": "sqlite"})

    assert response.status_code == 422


def test_settings_patch_strips_crlf_before_env_write() -> None:
    from unittest.mock import patch
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    with patch("steam_friend_relationship_map.app.set_key") as mock_set_key:
        response = client.patch("/api/settings", json={"neo4j_user": "neo4j\r\nINJECTED=1"})

    assert response.status_code == 200
    mock_set_key.assert_called_once()
    args, _ = mock_set_key.call_args
    assert args[1] == "NEO4J_USER"
    assert args[2] == "neo4jINJECTED=1"


def test_settings_patch_rolls_back_partial_env_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    writes: list[tuple[str, str]] = []

    def flaky_set_key(
        _path: str,
        key: str,
        value: str,
        **_: object,
    ) -> None:
        writes.append((key, value))
        if key == "DEFAULT_MAX_NODES" and value == "3000":
            raise OSError("disk write failed")

    monkeypatch.setattr(app_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app_module, "set_key", flaky_set_key)
    app = create_app(
        settings=Settings(default_max_depth=2, default_max_nodes=2000),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.patch(
        "/api/settings",
        json={"default_max_depth": 3, "default_max_nodes": 3000},
    )

    assert response.status_code == 500
    assert "disk write failed" in response.json()["detail"]
    assert writes == [
        ("DEFAULT_MAX_DEPTH", "3"),
        ("DEFAULT_MAX_NODES", "3000"),
        ("DEFAULT_MAX_DEPTH", "2"),
        ("DEFAULT_MAX_NODES", "2000"),
    ]


def test_settings_patch_keeps_current_repo_when_new_database_fails() -> None:
    old_settings = Settings().model_copy(update={"kuzu_db_path": "data/current"})
    new_settings = Settings().model_copy(update={"kuzu_db_path": "data/missing"})
    old_repo = TrackingRepo()
    candidate_repo = TrackingRepo(RuntimeError("unable to open database"))

    with (
        patch("steam_friend_relationship_map.app.get_repository", side_effect=[old_repo, candidate_repo]),
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
        patch("steam_friend_relationship_map.app.set_key") as mock_set_key,
    ):
        app = create_app(settings=old_settings, steam=FakeSteam(), secret_store=FakeSecretStore())  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.patch("/api/settings", json={"kuzu_db_path": "data/missing"})

    assert response.status_code == 400
    assert "configuration was not applied" in response.json()["detail"]
    assert app.state.repo is old_repo
    assert old_repo.close_count == 0
    assert candidate_repo.close_count == 1
    assert mock_set_key.call_args_list[-1].args[2] == "data/current"


def test_settings_patch_restores_same_kuzu_database_after_failure() -> None:
    old_settings = Settings().model_copy(
        update={"kuzu_db_path": "data/current", "kuzu_buffer_pool_size_gb": 1}
    )
    new_settings = Settings().model_copy(
        update={"kuzu_db_path": "data/current", "kuzu_buffer_pool_size_gb": 2}
    )
    old_repo = TrackingRepo()
    candidate_repo = TrackingRepo(RuntimeError("buffer pool is full"))
    restored_repo = TrackingRepo()

    with (
        patch(
            "steam_friend_relationship_map.app.get_repository",
            side_effect=[old_repo, candidate_repo, restored_repo],
        ),
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
        patch("steam_friend_relationship_map.app.set_key") as mock_set_key,
    ):
        app = create_app(settings=old_settings, steam=FakeSteam(), secret_store=FakeSecretStore())  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.patch("/api/settings", json={"kuzu_buffer_pool_size_gb": 2})

    assert response.status_code == 400
    assert app.state.repo is restored_repo
    assert old_repo.close_count == 1
    assert candidate_repo.close_count == 1
    assert restored_repo.close_count == 0
    assert mock_set_key.call_args_list[-1].args[2] == "1"


def test_settings_patch_swaps_repo_only_after_candidate_is_ready() -> None:
    old_settings = Settings().model_copy(update={"kuzu_db_path": "data/current"})
    new_settings = Settings().model_copy(update={"kuzu_db_path": "data/new"})
    old_repo = TrackingRepo()
    candidate_repo = TrackingRepo()

    with (
        patch("steam_friend_relationship_map.app.get_repository", side_effect=[old_repo, candidate_repo]),
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
        patch("steam_friend_relationship_map.app.set_key"),
    ):
        app = create_app(settings=old_settings, steam=FakeSteam(), secret_store=FakeSecretStore())  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.patch("/api/settings", json={"kuzu_db_path": "data/new"})

    assert response.status_code == 200
    assert app.state.repo is candidate_repo
    assert old_repo.close_count == 1
    assert candidate_repo.close_count == 0


def test_runtime_switch_keeps_ready_database_when_old_cleanup_fails() -> None:
    class CleanupFailingRepo(TrackingRepo):
        def close(self) -> None:
            self.close_count += 1
            raise RuntimeError("cleanup exposed-old-password")

    old_settings = Settings(
        graph_db_engine="neo4j",
        neo4j_uri="bolt://old-host:7687",
        neo4j_password="exposed-old-password",
    )
    new_settings = old_settings.model_copy(
        update={"neo4j_uri": "bolt://new-host:7687"}
    )
    old_repo = CleanupFailingRepo()
    candidate_repo = TrackingRepo()

    with (
        patch(
            "steam_friend_relationship_map.app.get_repository",
            side_effect=[old_repo, candidate_repo],
        ),
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
        patch("steam_friend_relationship_map.app.set_key"),
    ):
        app = create_app(
            settings=old_settings,
            steam=FakeSteam(),
            secret_store=FakeSecretStore(),
        )  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.patch(
            "/api/settings",
            json={"neo4j_uri": "bolt://new-host:7687"},
        )

    assert response.status_code == 200
    assert app.state.repo is candidate_repo
    assert old_repo.close_count == 1
    assert candidate_repo.close_count == 0
    logs = client.get("/api/logs").json()
    cleanup_log = next(
        row for row in logs if "Previous graph database cleanup failed" in row["message"]
    )
    assert "exposed-old-password" not in cleanup_log["message"]
    assert "[REDACTED]" in cleanup_log["message"]


def test_runtime_switch_closes_database_candidate_when_steam_creation_fails() -> None:
    old_settings = Settings(
        kuzu_db_path="data/current",
        steam_api_key="old-key",
    )
    new_settings = old_settings.model_copy(
        update={
            "kuzu_db_path": "data/new",
            "steam_api_key": "new-key",
        }
    )
    old_repo = TrackingRepo()
    candidate_repo = TrackingRepo()
    old_steam = MagicMock()
    old_steam.aclose = AsyncMock()
    store = FakeSecretStore()
    store.values["steam_api_key"] = "old-key"

    with (
        patch(
            "steam_friend_relationship_map.app.get_repository",
            side_effect=[old_repo, candidate_repo],
        ),
        patch(
            "steam_friend_relationship_map.app.get_settings",
            side_effect=[new_settings, old_settings],
        ),
        patch("steam_friend_relationship_map.app.set_key"),
        patch(
            "steam_friend_relationship_map.app.SteamClient",
            side_effect=[old_steam, RuntimeError("Steam client creation failed")],
        ),
    ):
        app = create_app(
            settings=old_settings,
            secret_store=store,
        )  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.put(
            "/api/settings",
            json={"kuzu_db_path": "data/new", "steam_api_key": "new-key"},
        )

    assert response.status_code == 400
    assert "Steam client creation failed" in response.json()["detail"]
    assert app.state.repo is old_repo
    assert app.state.steam is old_steam
    assert old_repo.close_count == 0
    assert candidate_repo.close_count == 1
    old_steam.aclose.assert_not_awaited()
    assert store.values["steam_api_key"] == "old-key"


def test_runtime_switch_keeps_ready_steam_client_when_old_cleanup_fails() -> None:
    old_settings = Settings(steam_api_key="old-steam-secret")
    new_settings = old_settings.model_copy(
        update={"steam_api_key": "new-steam-secret"}
    )
    old_steam = MagicMock()
    old_steam.aclose = AsyncMock(
        side_effect=RuntimeError("cleanup old-steam-secret")
    )
    new_steam = MagicMock()
    new_steam.aclose = AsyncMock()
    store = FakeSecretStore()
    store.values["steam_api_key"] = "old-steam-secret"

    with (
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
        patch(
            "steam_friend_relationship_map.app.SteamClient",
            side_effect=[old_steam, new_steam],
        ),
    ):
        app = create_app(
            settings=old_settings,
            repo=FakeRepo(),
            secret_store=store,
        )  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.post(
            "/api/settings/secrets",
            json={"name": "steam_api_key", "value": "new-steam-secret"},
        )

    assert response.status_code == 200
    assert app.state.steam is new_steam
    old_steam.aclose.assert_awaited_once()
    new_steam.aclose.assert_not_awaited()
    assert store.values["steam_api_key"] == "new-steam-secret"
    logs = client.get("/api/logs").json()
    cleanup_log = next(
        row for row in logs if "Previous Steam client cleanup failed" in row["message"]
    )
    assert "old-steam-secret" not in cleanup_log["message"]
    assert "[REDACTED]" in cleanup_log["message"]


def test_secret_update_restores_previous_value_when_database_reconnect_fails() -> None:
    old_settings = Settings().model_copy(
        update={"graph_db_engine": "neo4j", "neo4j_password": "old-secret"}
    )
    new_settings = old_settings.model_copy(update={"neo4j_password": "bad-secret"})
    old_repo = TrackingRepo()
    candidate_repo = TrackingRepo(RuntimeError("authentication failed"))
    secret_store = FakeSecretStore()
    secret_store.values["neo4j_password"] = "old-secret"

    with (
        patch("steam_friend_relationship_map.app.get_repository", side_effect=[old_repo, candidate_repo]),
        patch("steam_friend_relationship_map.app.get_settings", return_value=new_settings),
    ):
        app = create_app(settings=old_settings, steam=FakeSteam(), secret_store=secret_store)  # type: ignore[arg-type]
        client = TestClient(app)
        response = client.post(
            "/api/settings/secrets",
            json={"name": "neo4j_password", "value": "bad-secret"},
        )

    assert response.status_code == 400
    assert app.state.repo is old_repo
    assert old_repo.close_count == 0
    assert candidate_repo.close_count == 1
    assert secret_store.values["neo4j_password"] == "old-secret"


def test_csrf_rejects_localhost_prefix_spoof() -> None:
    app = create_app(settings=Settings(app_host="127.0.0.1", app_port=8000), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post(
        "/api/settings/test",
        json={},
        headers={"Origin": "http://localhost:8000.evil.example"},
    )

    assert response.status_code == 403


def test_csrf_allows_exact_localhost_origin() -> None:
    app = create_app(settings=Settings(app_host="127.0.0.1", app_port=8000), repo=FakeRepo(), steam=FakeSteam(), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post(
        "/api/settings/test",
        json={},
        headers={"Origin": "http://localhost:8000"},
    )

    assert response.status_code == 200


def test_settings_test_reports_missing_steam_key() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=FakeSteam(SteamApiError("缺少 STEAM_API_KEY")), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/settings/test", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["steam_ok"] is False
    assert body["steam_reason"] == "missing_key"
    assert "未配置 Steam API Key" in body["steam_message"]


def test_settings_test_reports_invalid_steam_key() -> None:
    app = create_app(settings=Settings(steam_api_key="bad-key"), repo=FakeRepo(), steam=FakeSteam(SteamApiError("Steam API 请求失败: HTTP 403", 403)), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/settings/test", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["steam_ok"] is False
    assert body["steam_reason"] == "invalid_key"
    assert "Steam API Key 无效" in body["steam_message"]


def test_settings_test_reports_neo4j_server_unavailable() -> None:
    app = create_app(
        settings=Settings(STEAM_API_KEY="key", NEO4J_PASSWORD="pw"),
        repo=FailingRepo(RuntimeError("Failed to establish connection to localhost:7687")),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/settings/test", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["neo4j_ok"] is False
    assert body["neo4j_reason"] == "server_unavailable"
    assert "请确认 Neo4j Desktop/Server 已启动" in body["neo4j_message"]


def test_logs_endpoint_redacts_sensitive_values() -> None:
    proxy_url = "http://proxy-user:proxy-secret@127.0.0.1:8080"
    app = create_app(settings=Settings(steam_api_key="abcd1234abcd1234abcd1234abcd1234", steam_proxy_url=proxy_url, neo4j_password="pw-secret"), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    app.state.logs.append(
        "error",
        "test",
        f"password=pw-secret key=abcd1234abcd1234abcd1234abcd1234 proxy={proxy_url} fallback=socks5://other-user:other-password@127.0.0.1:1080 Authorization: Bearer token123 Cookie: sid=abc",
    )
    response = client.get("/api/logs")

    assert response.status_code == 200
    text = response.text
    assert "pw-secret" not in text
    assert "abcd1234abcd1234abcd1234abcd1234" not in text
    assert "proxy-secret" not in text
    assert "other-password" not in text
    assert "token123" not in text
    assert "sid=abc" not in text
    assert "[REDACTED]" in text


def test_friend_circle_analysis_endpoint() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/analysis/friend-circles?root=root&max_depth=3&min_mutual=2&limit=10")

    assert response.status_code == 200
    assert response.json()["candidates"][0]["steam_id"] == "candidate"


def test_network_analysis_endpoint() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/analysis/network?limit=10")

    assert response.status_code == 200
    body = response.json()
    assert body["analyzed_nodes"] == 1
    assert body["analyzed_edges"] == 0
    assert body["community_count"] == 1
    assert body["leaders"][0]["id"] == "root"
    assert body["leaders"][0]["pagerank"] == 1


def test_network_analysis_errors_return_json_detail() -> None:
    class FailingExportRepo(FakeRepo):
        def export_graph(self, project_id: str = "default") -> ExportResponse:
            raise RuntimeError("analysis export failed")

    app = create_app(settings=Settings(), repo=FailingExportRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/analysis/network")

    assert response.status_code == 500
    assert response.json()["detail"] == "analysis export failed"


async def test_cancelled_network_analysis_finishes_before_next_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[int] = []

    def blocking_analysis(*_: object, **__: object) -> NetworkAnalysisResponse:
        calls.append(threading.get_ident())
        if len(calls) == 1:
            first_started.set()
            release_first.wait(timeout=2)
        return NetworkAnalysisResponse()

    monkeypatch.setattr(app_module, "analyze_network", blocking_analysis)
    app = create_app(
        settings=Settings(),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, release_first.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_request = asyncio.create_task(client.get("/api/analysis/network"))
            deadline = time.monotonic() + 0.5
            while not first_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert first_started.is_set()

            first_request.cancel()
            second_request = asyncio.create_task(client.get("/api/analysis/network"))
            await asyncio.sleep(0.05)
            assert len(calls) == 1
            assert not second_request.done()

            release_first.set()
            first_result = await asyncio.gather(
                first_request,
                return_exceptions=True,
            )
            second_response = await asyncio.wait_for(second_request, timeout=1)
    finally:
        release_first.set()
        safety_release.cancel()

    assert isinstance(first_result[0], asyncio.CancelledError)
    assert second_response.status_code == 200
    assert len(calls) == 2


async def test_lifespan_waits_for_detached_network_analysis_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    analysis_started = threading.Event()
    release_analysis = threading.Event()

    def failing_analysis(*_: object, **__: object) -> NetworkAnalysisResponse:
        analysis_started.set()
        release_analysis.wait(timeout=2)
        raise RuntimeError("analysis worker failed")

    class CloseTrackingRepo(FakeRepo):
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(app_module, "analyze_network", failing_analysis)
    repo = CloseTrackingRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, release_analysis.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            request = asyncio.create_task(client.get("/api/analysis/network"))
            deadline = time.monotonic() + 0.5
            while not analysis_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert analysis_started.is_set()
            request.cancel()
            result = await asyncio.gather(request, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)

        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.sleep(0.05)
        assert not shutdown.done()
        assert repo.closed is False

        release_analysis.set()
        await asyncio.wait_for(shutdown, timeout=1)
    finally:
        release_analysis.set()
        safety_release.cancel()

    assert repo.closed is True
    assert app.state.network_analysis_tasks == set()
    assert any(
        row.source == "analysis" and "analysis worker failed" in row.message
        for row in app.state.logs.list()
    )


def test_export_csv_body_is_utf8_complete_and_formula_safe() -> None:
    class ExportRepo(FakeRepo):
        def export_graph(self, project_id: str = "default") -> ExportResponse:
            return ExportResponse(
                nodes=[
                    {
                        "steam_id": "=danger-id",
                        "persona_name": "=HYPERLINK(\"https://example.test\")",
                        "profile_url": "+https://example.test",
                        "avatar_full": "https://example.test/avatar.jpg",
                        "note": "@SUM(1+1)",
                        "tags": ["=tag", "普通"],
                        "category": "-malicious",
                        "depth_min": 2,
                        "friend_count": 42,
                        "friend_list_status": "public",
                        "prior_pool_link_count": 3,
                        "root_closeness_score": 9.5,
                        "project_id": project_id,
                    }
                ],
                edges=[{"source": "\tformula", "target": "\rformula"}],
            )

    app = create_app(
        settings=Settings(active_project="project-a"),
        repo=ExportRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/export", json={"format": "csv"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="steam_graph.csv"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 2
    node, edge = rows
    assert node["type"] == "node"
    assert node["project_id"] == "project-a"
    assert node["id"] == "'=danger-id"
    assert node["label"].startswith("'=HYPERLINK")
    assert node["profile_url"] == "'+https://example.test"
    assert node["note"] == "'@SUM(1+1)"
    assert node["tags"] == '["=tag", "普通"]'
    assert node["category"] == "'-malicious"
    assert node["depth"] == "2"
    assert node["friend_count"] == "42"
    assert node["prior_pool_link_count"] == "3"
    assert node["root_closeness_score"] == "9.5"
    assert edge["type"] == "edge"
    assert edge["source"] == "'\tformula"
    assert edge["target"] == "'\rformula"


def test_export_csv_iterator_emits_bounded_chunks() -> None:
    data = ExportResponse(
        nodes=[
            {
                "steam_id": str(index),
                "persona_name": f"User {index}",
                "note": "x" * 240,
                "project_id": "project-a",
            }
            for index in range(40)
        ],
        edges=[{"source": str(index), "target": str(index + 1)} for index in range(39)],
    )

    chunks = list(iter_export_csv(data, "project-a", chunk_size=1024))

    assert len(chunks) > 5
    assert chunks[0].startswith("\ufeff")
    assert max(map(len, chunks)) < 2048
    rows = list(csv.DictReader(io.StringIO("".join(chunks).lstrip("\ufeff"))))
    assert len(rows) == 79
    assert rows[0]["project_id"] == "project-a"
    assert rows[-1]["type"] == "edge"


def test_export_accepts_json_body_and_legacy_query_format() -> None:
    app = create_app(
        settings=Settings(),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    json_response = client.post("/api/export", json={"format": "json"})
    csv_response = client.post("/api/export?format=csv")
    invalid_body = client.post("/api/export", json={"format": "xml"})
    invalid_query = client.post("/api/export?format=xml")

    assert json_response.status_code == 200
    assert json_response.json()["nodes"][0]["steam_id"] == "root"
    assert json_response.headers["content-disposition"] == 'attachment; filename="steam_graph.json"'
    assert json_response.headers["cache-control"] == "no-store"
    assert json_response.headers["x-content-type-options"] == "nosniff"
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert invalid_body.status_code == 422
    assert invalid_query.status_code == 400


def test_project_switch_strips_crlf() -> None:
    from unittest.mock import patch
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    with patch("steam_friend_relationship_map.app.set_key") as mock_set_key:
        response = client.post("/api/projects/switch", json={"name": "test\r\ninjected\nname"})
        assert response.status_code == 200
        mock_set_key.assert_called_once()
        args, _ = mock_set_key.call_args
        assert args[1] == "ACTIVE_PROJECT"
        assert args[2] == "testinjectedname"


def test_project_switch_keeps_existing_repository_when_only_active_project_changes(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import steam_friend_relationship_map.app as app_module

    class TrackingRepo(FakeRepo):
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def list_projects(self) -> object:
            from steam_friend_relationship_map.models import ProjectInfo, ProjectListResponse
            return ProjectListResponse(
                projects=[
                    ProjectInfo(id="default", name="Default"),
                    ProjectInfo(id="project-a", name="Project A"),
                ],
                active_project_id="default",
            )

        def create_project(self, payload: object, project_id: str | None = None) -> str:
            return project_id or "project-a"

        def project_exists(self, project_id: str) -> bool:
            return project_id in {"default", "project-a"}

    repo_instances: list[TrackingRepo] = []
    initial_settings = Settings(active_project="default")
    switched_settings = Settings(active_project="project-a")

    def fake_get_repository(_: Settings) -> TrackingRepo:
        repo = TrackingRepo()
        repo_instances.append(repo)
        return repo

    monkeypatch.setattr(app_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app_module, "get_repository", fake_get_repository)
    monkeypatch.setattr(app_module, "get_settings", lambda: switched_settings)

    app = create_app(settings=initial_settings, steam=FakeSteam(), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/projects/switch", json={"name": "project-a"})

    assert response.status_code == 200
    assert response.json()["active_project_id"] == "project-a"
    assert len(repo_instances) == 1
    assert repo_instances[0].closed is False


def test_app_endpoints_blocked_during_crawl() -> None:
    from unittest.mock import MagicMock
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    
    app.state.manager.has_active_crawl = MagicMock(return_value=True)
    client = TestClient(app)
    
    # 1. Test patch settings
    resp = client.patch("/api/settings", json={"default_max_depth": 3})
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]
    
    # 2. Test set secret
    resp = client.post("/api/settings/secrets", json={"name": "steam_api_key", "value": "test"})
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]
    
    # 3. Test delete secret
    resp = client.delete("/api/settings/secrets/steam_api_key")
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]

    # 4. Test create project
    resp = client.post("/api/projects", json={"name": "test-project"})
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]

    # 5. Test delete project
    resp = client.delete("/api/projects/test-project")
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]

    # 6. Test switch project
    resp = client.post("/api/projects/switch", json={"name": "test-project"})
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]


def test_app_crawls_conflict_returns_409() -> None:
    from unittest.mock import AsyncMock
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    
    app.state.manager.create_crawl = AsyncMock(side_effect=RuntimeError("已有活跃的抓取任务在运行中"))
    client = TestClient(app)
    
    resp = client.post("/api/crawls", json={"root_url": "root", "max_depth": 2})
    assert resp.status_code == 409
    assert "已有活跃的抓取任务在运行中" in resp.json()["detail"]


def test_active_crawl_endpoint_returns_the_managed_run() -> None:
    active_run = CrawlRun(
        id="run-active",
        root_steam_id="root",
        max_depth=2,
        max_nodes=100,
        status=CrawlStatus.running,
    )

    class ActiveRunRepo(FakeRepo):
        def get_crawl_run(self, run_id: str) -> CrawlRun | None:
            return active_run if run_id == active_run.id else None

    app = create_app(
        settings=Settings(),
        repo=ActiveRunRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    app.state.manager.get_active_run_id = MagicMock(return_value=active_run.id)
    client = TestClient(app)

    response = client.get("/api/crawls/active")

    assert response.status_code == 200
    assert response.json()["id"] == active_run.id
    app.state.manager.get_active_run_id.return_value = None
    assert client.get("/api/crawls/active").json() is None


async def test_cancelled_crawl_creation_finishes_after_it_acquires_runtime_lock() -> None:
    app = create_app(
        settings=Settings(),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    creation_started = asyncio.Event()
    allow_creation = asyncio.Event()
    creation_finished = asyncio.Event()

    async def delayed_create_crawl(payload: object) -> CrawlRun:
        creation_started.set()
        await allow_creation.wait()
        creation_finished.set()
        return CrawlRun(
            id="run-after-disconnect",
            root_steam_id="root",
            max_depth=2,
            max_nodes=100,
            status=CrawlStatus.pending,
        )

    app.state.manager.create_crawl = delayed_create_crawl
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        request = asyncio.create_task(
            client.post(
                "/api/crawls",
                json={"root_url": "root", "max_depth": 2, "max_nodes": 100},
            )
        )
        await asyncio.wait_for(creation_started.wait(), timeout=1)
        request.cancel()
        await asyncio.sleep(0)
        allow_creation.set()
        await asyncio.wait_for(creation_finished.wait(), timeout=1)
        result = await asyncio.gather(request, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)


async def test_runtime_mutation_waits_for_crawl_creation() -> None:
    app = create_app(
        settings=Settings(),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    crawl_started = asyncio.Event()
    allow_crawl_creation = asyncio.Event()

    async def delayed_create_crawl(payload: object) -> CrawlRun:
        crawl_started.set()
        await allow_crawl_creation.wait()
        app.state.manager.has_active_crawl = MagicMock(return_value=True)
        return CrawlRun(
            id="run-locked",
            root_steam_id="root",
            max_depth=2,
            max_nodes=100,
            status=CrawlStatus.pending,
        )

    app.state.manager.create_crawl = delayed_create_crawl
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        crawl_request = asyncio.create_task(
            client.post(
                "/api/crawls",
                json={"root_url": "root", "max_depth": 2, "max_nodes": 100},
            )
        )
        await crawl_started.wait()
        settings_request = asyncio.create_task(
            client.patch("/api/settings", json={"default_max_depth": 3})
        )
        await asyncio.sleep(0)
        assert not settings_request.done()

        allow_crawl_creation.set()
        crawl_response, settings_response = await asyncio.gather(
            crawl_request, settings_request
        )

    assert crawl_response.status_code == 200
    assert settings_response.status_code == 400
    assert "当前有活跃的抓取任务在运行" in settings_response.json()["detail"]


async def test_heavy_repository_query_runs_off_loop_and_guards_runtime_access() -> None:
    class BlockingGraphRepo(FakeRepo):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.query_thread_id: int | None = None

        def get_graph(self, **kwargs: object) -> GraphResponse:
            self.query_thread_id = threading.get_ident()
            self.started.set()
            self.release.wait(timeout=2)
            return super().get_graph(**kwargs)

    repo = BlockingGraphRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    event_loop_thread_id = threading.get_ident()
    safety_release = threading.Timer(1, repo.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            graph_request = asyncio.create_task(client.get("/api/graph"))
            deadline = time.monotonic() + 0.5
            while not repo.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.started.is_set()

            health_request = asyncio.create_task(client.get("/api/health"))
            await asyncio.sleep(0.05)
            assert not health_request.done()

            logs_started = time.monotonic()
            logs_response = await asyncio.wait_for(client.get("/api/logs"), timeout=0.25)
            assert time.monotonic() - logs_started < 0.25
            assert logs_response.status_code == 200

            repo.release.set()
            graph_response, health_response = await asyncio.gather(graph_request, health_request)
    finally:
        repo.release.set()
        safety_release.cancel()

    assert graph_response.status_code == 200
    assert health_response.status_code == 200
    assert repo.query_thread_id != event_loop_thread_id


async def test_runtime_settings_rebuild_runs_blocking_work_off_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    class BlockingSchemaRepo(FakeRepo):
        def __init__(self) -> None:
            self.block_schema = False
            self.started = threading.Event()
            self.release = threading.Event()
            self.schema_thread_id: int | None = None

        def ensure_schema(self) -> None:
            if not self.block_schema:
                return
            self.schema_thread_id = threading.get_ident()
            self.started.set()
            self.release.wait(timeout=2)

    write_thread_ids: list[int] = []

    def record_set_key(*_: object, **__: object) -> None:
        write_thread_ids.append(threading.get_ident())

    repo = BlockingSchemaRepo()
    monkeypatch.setattr(app_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app_module, "set_key", record_set_key)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(default_max_depth=3),
    )
    app = create_app(
        settings=Settings(default_max_depth=2),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    repo.block_schema = True
    transport = ASGITransport(app=app)
    event_loop_thread_id = threading.get_ident()
    safety_release = threading.Timer(1, repo.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            settings_request = asyncio.create_task(
                client.patch("/api/settings", json={"default_max_depth": 3})
            )
            deadline = time.monotonic() + 0.5
            while not repo.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.started.is_set()
            assert not settings_request.done()

            logs_response = await asyncio.wait_for(client.get("/api/logs"), timeout=0.25)
            assert logs_response.status_code == 200

            repo.release.set()
            settings_response = await settings_request
    finally:
        repo.release.set()
        safety_release.cancel()

    assert settings_response.status_code == 200
    assert write_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in write_thread_ids)
    assert repo.schema_thread_id != event_loop_thread_id


async def test_public_settings_reads_secure_store_off_loop() -> None:
    class BlockingSecretStore(FakeSecretStore):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.read_thread_id: int | None = None

        def get(self, name: str) -> str:
            self.read_thread_id = threading.get_ident()
            self.started.set()
            self.release.wait(timeout=2)
            return super().get(name)

    store = BlockingSecretStore()
    app = create_app(
        settings=Settings(),
        repo=FakeRepo(),
        steam=FakeSteam(),
        secret_store=store,
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    event_loop_thread_id = threading.get_ident()
    safety_release = threading.Timer(1, store.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            settings_request = asyncio.create_task(client.get("/api/settings"))
            deadline = time.monotonic() + 0.5
            while not store.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert store.started.is_set()

            logs_response = await asyncio.wait_for(client.get("/api/logs"), timeout=0.25)
            assert logs_response.status_code == 200

            store.release.set()
            settings_response = await settings_request
    finally:
        store.release.set()
        safety_release.cancel()

    assert settings_response.status_code == 200
    assert store.read_thread_id != event_loop_thread_id


async def test_cancelled_repository_request_holds_lock_until_worker_finishes() -> None:
    class BlockingGraphRepo(FakeRepo):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def get_graph(self, **kwargs: object) -> GraphResponse:
            self.started.set()
            self.release.wait(timeout=2)
            return super().get_graph(**kwargs)

    repo = BlockingGraphRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, repo.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            graph_request = asyncio.create_task(client.get("/api/graph"))
            deadline = time.monotonic() + 0.5
            while not repo.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.started.is_set()

            graph_request.cancel()
            await asyncio.sleep(0.05)

            health_request = asyncio.create_task(client.get("/api/health"))
            await asyncio.sleep(0.05)
            assert not health_request.done()

            repo.release.set()
            with pytest.raises(asyncio.CancelledError):
                await graph_request
            health_response = await health_request
    finally:
        repo.release.set()
        safety_release.cancel()

    assert health_response.status_code == 200


async def test_cancelled_settings_test_holds_lock_until_worker_finishes() -> None:
    class BlockingConnectionRepo(FakeRepo):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.connection_calls = 0

        def test_connection(self) -> str:
            self.connection_calls += 1
            if self.connection_calls == 1:
                self.started.set()
                self.release.wait(timeout=2)
            return "connection ok"

    repo = BlockingConnectionRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, repo.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            settings_test = asyncio.create_task(
                client.post("/api/settings/test", json={})
            )
            deadline = time.monotonic() + 0.5
            while not repo.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.started.is_set()

            settings_test.cancel()
            with pytest.raises(asyncio.CancelledError):
                await settings_test

            health_request = asyncio.create_task(client.get("/api/health"))
            await asyncio.sleep(0.05)
            assert not health_request.done()

            repo.release.set()
            health_response = await health_request
    finally:
        repo.release.set()
        safety_release.cancel()

    assert health_response.status_code == 200
    assert repo.connection_calls == 2


async def test_cancelled_queued_repository_request_never_starts() -> None:
    class CountingBlockingRepo(FakeRepo):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def get_graph(self, **kwargs: object) -> GraphResponse:
            self.calls += 1
            self.started.set()
            self.release.wait(timeout=2)
            return super().get_graph(**kwargs)

    repo = CountingBlockingRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, repo.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_request = asyncio.create_task(client.get("/api/graph"))
            deadline = time.monotonic() + 0.5
            while not repo.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.started.is_set()

            queued_request = asyncio.create_task(client.get("/api/graph?depth=3"))
            await asyncio.sleep(0.05)
            queued_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued_request

            repo.release.set()
            first_response = await first_request
            await asyncio.sleep(0.05)
    finally:
        repo.release.set()
        safety_release.cancel()

    assert first_response.status_code == 200
    assert repo.calls == 1


async def test_cancelled_started_runtime_mutation_finishes_before_unlocking() -> None:
    class BlockingProjectRepo(FakeRepo):
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.created: list[str] = []

        def create_project(self, payload: object, project_id: str | None = None) -> str:
            self.started.set()
            self.release.wait(timeout=2)
            self.created.append(project_id or "created-project")
            return project_id or "created-project"

    repo = BlockingProjectRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, repo.release.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            mutation_request = asyncio.create_task(
                client.post("/api/projects", json={"name": "created-project"})
            )
            deadline = time.monotonic() + 0.5
            while not repo.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.started.is_set()

            mutation_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mutation_request

            health_request = asyncio.create_task(client.get("/api/health"))
            await asyncio.sleep(0.05)
            assert not health_request.done()

            repo.release.set()
            health_response = await health_request
    finally:
        repo.release.set()
        safety_release.cancel()

    assert health_response.status_code == 200
    assert repo.created == ["created-project"]


async def test_cancelled_queued_runtime_mutation_never_starts() -> None:
    class BlockingGraphAndProjectRepo(FakeRepo):
        def __init__(self) -> None:
            self.graph_started = threading.Event()
            self.release_graph = threading.Event()
            self.project_calls = 0

        def get_graph(self, **kwargs: object) -> GraphResponse:
            self.graph_started.set()
            self.release_graph.wait(timeout=2)
            return super().get_graph(**kwargs)

        def create_project(self, payload: object, project_id: str | None = None) -> str:
            self.project_calls += 1
            return project_id or "queued-project"

    repo = BlockingGraphAndProjectRepo()
    app = create_app(
        settings=Settings(),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    safety_release = threading.Timer(1, repo.release_graph.set)
    safety_release.start()

    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            graph_request = asyncio.create_task(client.get("/api/graph"))
            deadline = time.monotonic() + 0.5
            while not repo.graph_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert repo.graph_started.is_set()

            mutation_request = asyncio.create_task(
                client.post("/api/projects", json={"name": "queued-project"})
            )
            await asyncio.sleep(0.05)
            mutation_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mutation_request

            repo.release_graph.set()
            graph_response = await graph_request
            await asyncio.sleep(0.05)
    finally:
        repo.release_graph.set()
        safety_release.cancel()

    assert graph_response.status_code == 200
    assert repo.project_calls == 0


def test_project_switch_rolls_back_auto_created_project_on_reload_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    class ProjectTrackingRepo(FakeRepo):
        def __init__(self) -> None:
            self.created: list[str] = []
            self.deleted: list[str] = []

        def project_exists(self, project_id: str) -> bool:
            return project_id == "default"

        def create_project(self, payload: object, project_id: str | None = None) -> str:
            assert project_id is not None
            self.created.append(project_id)
            return project_id

        def delete_project(self, project_id: str) -> bool:
            self.deleted.append(project_id)
            return True

    repo = ProjectTrackingRepo()
    env_path = tmp_path / ".env"
    monkeypatch.setattr(app_module, "ENV_PATH", env_path)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        MagicMock(side_effect=RuntimeError("settings reload failed")),
    )
    app = create_app(
        settings=Settings(active_project="default"),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post(
        "/api/projects/switch",
        json={"project_id": "project-new"},
    )

    assert response.status_code == 400
    assert "settings reload failed" in response.json()["detail"]
    assert repo.created == ["project-new"]
    assert repo.deleted == ["project-new"]
    assert "ACTIVE_PROJECT=default" in env_path.read_text(encoding="utf-8")


def test_active_project_delete_does_not_start_when_default_switch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    class DeleteTrackingRepo(FakeRepo):
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_project(self, project_id: str) -> bool:
            self.deleted.append(project_id)
            return True

    env_path = tmp_path / ".env"
    env_path.write_text("ACTIVE_PROJECT=project-a\n", encoding="utf-8")
    events: list[str] = []

    def write_active_project(
        _path: str, _key: str, value: str, *, quote_mode: str
    ) -> None:
        assert quote_mode == "never"
        events.append(f"set:{value}")
        if value == "default":
            raise OSError("env write failed")
        env_path.write_text(f"ACTIVE_PROJECT={value}\n", encoding="utf-8")

    def load_settings() -> Settings:
        active = env_path.read_text(encoding="utf-8").strip().split("=", 1)[1]
        return Settings(active_project=active)

    repo = DeleteTrackingRepo()
    monkeypatch.setattr(app_module, "ENV_PATH", env_path)
    monkeypatch.setattr(app_module, "set_key", write_active_project)
    monkeypatch.setattr(app_module, "get_settings", load_settings)
    app = create_app(
        settings=Settings(active_project="project-a"),
        repo=repo,
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.delete("/api/projects/project-a")

    assert response.status_code == 400
    assert repo.deleted == []
    assert events == ["set:default", "set:project-a"]
    assert "ACTIVE_PROJECT=project-a" in env_path.read_text(encoding="utf-8")
    assert client.get("/api/health").json()["project_id"] == "project-a"


def test_active_project_delete_failure_restores_previous_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    env_path = tmp_path / ".env"
    env_path.write_text("ACTIVE_PROJECT=project-a\n", encoding="utf-8")
    events: list[str] = []

    class FailingDeleteRepo(FakeRepo):
        def delete_project(self, project_id: str) -> bool:
            events.append(f"delete:{project_id}")
            raise RuntimeError("transaction rolled back")

    def write_active_project(
        _path: str, _key: str, value: str, *, quote_mode: str
    ) -> None:
        assert quote_mode == "never"
        events.append(f"set:{value}")
        env_path.write_text(f"ACTIVE_PROJECT={value}\n", encoding="utf-8")

    def load_settings() -> Settings:
        active = env_path.read_text(encoding="utf-8").strip().split("=", 1)[1]
        return Settings(active_project=active)

    monkeypatch.setattr(app_module, "ENV_PATH", env_path)
    monkeypatch.setattr(app_module, "set_key", write_active_project)
    monkeypatch.setattr(app_module, "get_settings", load_settings)
    app = create_app(
        settings=Settings(active_project="project-a"),
        repo=FailingDeleteRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.delete("/api/projects/project-a")

    assert response.status_code == 500
    assert "transaction rolled back" in response.json()["detail"]
    assert events == ["set:default", "delete:project-a", "set:project-a"]
    assert "ACTIVE_PROJECT=project-a" in env_path.read_text(encoding="utf-8")
    assert client.get("/api/health").json()["project_id"] == "project-a"


def test_active_project_delete_switches_to_default_before_removing_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steam_friend_relationship_map import app as app_module

    env_path = tmp_path / ".env"
    env_path.write_text("ACTIVE_PROJECT=project-a\n", encoding="utf-8")
    events: list[str] = []

    class SuccessfulDeleteRepo(FakeRepo):
        def delete_project(self, project_id: str) -> bool:
            events.append(f"delete:{project_id}")
            return True

    def write_active_project(
        _path: str, _key: str, value: str, *, quote_mode: str
    ) -> None:
        assert quote_mode == "never"
        events.append(f"set:{value}")
        env_path.write_text(f"ACTIVE_PROJECT={value}\n", encoding="utf-8")

    def load_settings() -> Settings:
        active = env_path.read_text(encoding="utf-8").strip().split("=", 1)[1]
        return Settings(active_project=active)

    monkeypatch.setattr(app_module, "ENV_PATH", env_path)
    monkeypatch.setattr(app_module, "set_key", write_active_project)
    monkeypatch.setattr(app_module, "get_settings", load_settings)
    app = create_app(
        settings=Settings(active_project="project-a"),
        repo=SuccessfulDeleteRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.delete("/api/projects/project-a")

    assert response.status_code == 200
    assert events == ["set:default", "delete:project-a"]
    assert "ACTIVE_PROJECT=default" in env_path.read_text(encoding="utf-8")
    assert client.get("/api/health").json()["project_id"] == "default"


def test_app_crawls_reject_out_of_range_request_concurrency() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post(
        "/api/crawls",
        json={"root_url": "root", "max_depth": 2, "request_concurrency": 17},
    )

    assert response.status_code == 422


def test_app_lifespan_closes_resources_in_dependency_order() -> None:
    events: list[str] = []

    class LifecycleRepo(FakeRepo):
        def close(self) -> None:
            events.append("repo")

    class LifecycleSteam(FakeSteam):
        async def aclose(self) -> None:
            events.append("steam")

    app = create_app(
        settings=Settings(active_project="default"),
        repo=LifecycleRepo(),
        steam=LifecycleSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    app.state.manager.shutdown = AsyncMock(side_effect=lambda: events.append("manager"))

    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "database": "kuzu",
            "database_message": "Neo4j 连接正常",
            "active_crawl": False,
            "project_id": "default",
        }

    assert events == ["manager", "steam", "repo"]


def test_app_lifespan_releases_log_buffer_when_resource_cleanup_fails() -> None:
    class CloseFailingRepo(FakeRepo):
        def close(self) -> None:
            raise RuntimeError("close failed")

    app = create_app(
        settings=Settings(),
        repo=CloseFailingRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]
    handler = next(
        candidate
        for candidate in logging.getLogger("steam_friend_relationship_map").handlers
        if isinstance(candidate, AppLogHandler)
    )
    assert handler.buffer is app.state.logs

    with pytest.raises(RuntimeError, match="close failed"):
        with TestClient(app):
            pass

    assert handler.buffer is not app.state.logs


def test_health_returns_503_when_database_is_unavailable() -> None:
    class UnhealthyRepo(FakeRepo):
        def test_connection(self) -> str:
            raise RuntimeError("database offline")

    app = create_app(
        settings=Settings(),
        repo=UnhealthyRepo(),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["database_message"] == "database offline"


def test_app_lifespan_releases_kuzu_database_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "app-lifecycle-kuzu"
    app = create_app(
        settings=Settings(
            graph_db_engine="kuzu",
            kuzu_db_path=str(db_path),
            active_project="default",
        ),
        steam=FakeSteam(),
        secret_store=FakeSecretStore(),
    )  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["database"] == "kuzu"

    reopened = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
    try:
        assert reopened.test_connection() == "Kùzu 连接正常"
    finally:
        reopened.close()

