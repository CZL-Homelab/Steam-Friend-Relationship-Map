from __future__ import annotations

from fastapi.testclient import TestClient

from steam_friend_relationship_map.app import create_app
from steam_friend_relationship_map.models import DbStats, ExportResponse, FriendCircleAnalysisResponse, FriendCircleCandidate, GraphEdge, GraphNode, GraphResponse
from steam_friend_relationship_map.settings import Settings
from steam_friend_relationship_map.steam import SteamClient


class FakeRepo:
    def close(self) -> None:
        pass

    def get_graph(self, **_: object) -> GraphResponse:
        return GraphResponse(
            nodes=[GraphNode(id="root", label="Root", degree=1)],
            edges=[GraphEdge(id="root-a", source="root", target="a")],
        )

    def patch_user(self, *_: object, **__: object) -> None:
        pass

    def get_shortest_path(self, *_: object, **__: object) -> GraphResponse:
        return GraphResponse(nodes=[GraphNode(id="root", label="Root")], edges=[])

    def get_top_degree(self, limit: int = 12) -> list[GraphNode]:
        return [GraphNode(id="root", label="Root", degree=5)]

    def get_db_stats(self) -> DbStats:
        return DbStats(steam_users=2, steam_friend_relationships=1, crawl_runs=1)

    def get_friend_circle_analysis(self, **_: object) -> FriendCircleAnalysisResponse:
        return FriendCircleAnalysisResponse(
            root="root",
            candidates=[FriendCircleCandidate(steam_id="candidate", label="Candidate", mutual_count=2, score=18)],
        )

    def export_graph(self) -> ExportResponse:
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
