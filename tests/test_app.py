from __future__ import annotations

from fastapi.testclient import TestClient

from steam_friend_relationship_map.app import create_app
from steam_friend_relationship_map.models import DbStats, ExportResponse, FriendCircleAnalysisResponse, FriendCircleCandidate, GraphEdge, GraphNode, GraphResponse
from steam_friend_relationship_map.settings import Settings
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

    def get_graph(self, **_: object) -> GraphResponse:
        return GraphResponse(
            nodes=[GraphNode(id="root", label="Root", degree=1)],
            edges=[GraphEdge(id="root-a", source="root", target="a")],
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


def test_graph_endpoint_uses_repo() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/graph?root=root&depth=2&limit=50")

    assert response.status_code == 200
    assert response.json()["nodes"][0]["id"] == "root"


def test_user_patch_endpoint() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.patch("/api/users/root", json={"note": "friend", "tags": ["cs2", "cs2"], "category": "game"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


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
    app = create_app(settings=Settings(steam_api_key="abcd1234abcd1234abcd1234abcd1234", neo4j_password="pw-secret"), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    app.state.logs.append("error", "test", "password=pw-secret key=abcd1234abcd1234abcd1234abcd1234")
    response = client.get("/api/logs")

    assert response.status_code == 200
    text = response.text
    assert "pw-secret" not in text
    assert "abcd1234abcd1234abcd1234abcd1234" not in text
    assert "[REDACTED]" in text


def test_friend_circle_analysis_endpoint() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/analysis/friend-circles?root=root&max_depth=3&min_mutual=2&limit=10")

    assert response.status_code == 200
    assert response.json()["candidates"][0]["steam_id"] == "candidate"
