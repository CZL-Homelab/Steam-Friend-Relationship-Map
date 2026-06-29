from __future__ import annotations

from fastapi.testclient import TestClient

from steam_friend_relationship_map.app import create_app
from steam_friend_relationship_map.models import DbStats, ExportResponse, FriendCircleAnalysisResponse, FriendCircleCandidate, GraphEdge, GraphNode, GraphResponse
from steam_friend_relationship_map.settings import Settings
from steam_friend_relationship_map.steam import SteamClient


class FakeRepo:
    def close(self) -> None:
        pass

    def ensure_schema(self) -> None:
        pass

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

