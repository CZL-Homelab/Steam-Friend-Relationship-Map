from __future__ import annotations

from fastapi.testclient import TestClient

import steam_friend_relationship_map.app as app_module
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

    def get_potential_friends(self, **_: object) -> PotentialFriendsResponse:
        from steam_friend_relationship_map.models import PotentialFriendCandidate, PotentialFriendsResponse
        return PotentialFriendsResponse(
            root="root",
            candidates=[PotentialFriendCandidate(steam_id="candidate", label="Candidate", mutual_count=2, jaccard_coefficient=0.5, score=50.0)],
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
    body = response.json()
    assert body["nodes"][0]["id"] == "root"
    assert body["requested_depth"] == 2
    assert body["traversal_depth_reached"] == 1
    assert body["root_found"] is True
    assert body["depth_incomplete"] is True
    assert body["nodes"][0]["root_route_count"] == 1
    assert body["nodes"][0]["root_route_total_hops"] == 0
    assert body["nodes"][0]["root_friend_circle_score"] == 1_000_000


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


def test_secret_api_rejects_unknown_secret_name() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post("/api/settings/secrets", json={"name": "cookie", "value": "secret"})

    assert response.status_code == 422


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
    app = create_app(settings=Settings(steam_api_key="abcd1234abcd1234abcd1234abcd1234", neo4j_password="pw-secret"), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    app.state.logs.append(
        "error",
        "test",
        "password=pw-secret key=abcd1234abcd1234abcd1234abcd1234 Authorization: Bearer token123 Cookie: sid=abc",
    )
    response = client.get("/api/logs")

    assert response.status_code == 200
    text = response.text
    assert "pw-secret" not in text
    assert "abcd1234abcd1234abcd1234abcd1234" not in text
    assert "token123" not in text
    assert "sid=abc" not in text
    assert "[REDACTED]" in text


def test_friend_circle_analysis_endpoint() -> None:
    app = create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/analysis/friend-circles?root=root&max_depth=3&min_mutual=2&limit=10")

    assert response.status_code == 200
    assert response.json()["candidates"][0]["steam_id"] == "candidate"


def test_potential_friends_endpoint() -> None:
    app = app_module.create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get("/api/analysis/potential-friends?root=root&max_depth=3&min_mutual=2&limit=10")

    assert response.status_code == 200
    assert response.json()["candidates"][0]["steam_id"] == "candidate"
    assert response.json()["candidates"][0]["jaccard_coefficient"] == 0.5



def test_project_switch_strips_crlf() -> None:
    from unittest.mock import patch
    app = app_module.create_app(settings=Settings(), repo=FakeRepo(), steam=SteamClient("key"), secret_store=FakeSecretStore())  # type: ignore[arg-type]
    client = TestClient(app)

    with patch("steam_friend_relationship_map.app.set_key") as mock_set_key:
        response = client.post("/api/projects/switch", json={"name": "test\r\ninjected\nname"})
        assert response.status_code == 200
        mock_set_key.assert_called_once()
        args, _ = mock_set_key.call_args
        assert args[1] == "ACTIVE_PROJECT"
        assert args[2] == "testinjectedname"


def test_project_switch_keeps_existing_repository_when_only_active_project_changes(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]

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

    app = app_module.create_app(settings=initial_settings, steam=FakeSteam(), secret_store=FakeSecretStore())  # type: ignore[arg-type]
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

    # 4. Test delete project
    resp = client.delete("/api/projects/test-project")
    assert resp.status_code == 400
    assert "当前有活跃的抓取任务在运行" in resp.json()["detail"]

    # 5. Test switch project
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

