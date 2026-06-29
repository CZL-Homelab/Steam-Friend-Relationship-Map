from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from steam_friend_relationship_map.kuzu_repo import KuzuRepositoryImpl
from steam_friend_relationship_map.models import (
    CrawlRun,
    CrawlStatus,
    FriendEdge,
    ProjectCreate,
    SteamUserRecord,
)


@pytest.fixture
def temp_kuzu_repo() -> Generator[KuzuRepositoryImpl, None, None]:
    db_dir = tempfile.mkdtemp()
    db_path = Path(db_dir) / "kuzu_db"
    repo = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
    yield repo
    # Cleanup
    shutil.rmtree(db_dir)


def test_kuzu_lifecycle_and_schema(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    # 1. test connection
    assert repo.test_connection() == "Kùzu 连接正常"

    # 2. ensure schema
    repo.ensure_schema()

    # 3. default project
    pid = repo.ensure_default_project()
    assert pid == "default"
    assert repo.project_exists("default")

    # 4. create project
    p1 = repo.create_project(ProjectCreate(name="测试项目"), project_id="test-1")
    assert p1 == "test-1"
    assert repo.project_exists("test-1")

    # 5. list projects
    projects = repo.list_projects().projects
    assert len(projects) >= 2

    # 6. delete project
    assert repo.delete_project("test-1")
    assert not repo.project_exists("test-1")


def test_kuzu_crawl_runs(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    run = CrawlRun(
        id="run-1",
        root_steam_id="76561198000000000",
        max_depth=2,
        max_nodes=100,
        status=CrawlStatus.pending,
    )
    repo.start_crawl_run(run, "default")

    retrieved = repo.get_crawl_run("run-1")
    assert retrieved is not None
    assert retrieved.root_steam_id == "76561198000000000"
    assert retrieved.status == CrawlStatus.pending

    repo.update_crawl_run("run-1", status=CrawlStatus.completed.value, nodes_discovered=10)
    retrieved2 = repo.get_crawl_run("run-1")
    assert retrieved2 is not None
    assert retrieved2.status == CrawlStatus.completed
    assert retrieved2.nodes_discovered == 10


def test_kuzu_graph_operations(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    # Upsert users
    users = [
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="2", persona_name="Bob", depth_min=1),
        SteamUserRecord(steam_id="3", persona_name="Charlie", depth_min=2),
    ]
    repo.upsert_users(users, "default")

    # Create relationships
    edges = [
        FriendEdge(from_id="1", to_id="2", crawl_id="run-1", source_depth=0),
        FriendEdge(from_id="2", to_id="3", crawl_id="run-1", source_depth=1),
    ]
    repo.upsert_relationships(edges, "default")

    # Check top degree
    top = repo.get_top_degree(limit=5, project_id="default")
    assert len(top) == 3
    bob_node = [n for n in top if n.id == "2"][0]
    assert bob_node.degree == 2

    # Get graph
    graph = repo.get_graph(root="1", depth=2, limit=10, project_id="default")
    assert len(graph.nodes) == 3
    assert len(graph.edges) == 2

    # Shortest path
    path = repo.get_shortest_path(from_id="1", to_id="3", max_depth=3, project_id="default")
    assert len(path.nodes) == 3
    assert path.nodes[0].id == "1"
    assert path.nodes[1].id == "2"
    assert path.nodes[2].id == "3"

    # Friend circle analysis
    # Let's add user 4 who is friend with 2 and 3, but not 1
    repo.upsert_users([SteamUserRecord(steam_id="4", persona_name="David", depth_min=2)], "default")
    repo.upsert_relationships([
        FriendEdge(from_id="2", to_id="4", crawl_id="run-1", source_depth=1),
        FriendEdge(from_id="3", to_id="4", crawl_id="run-1", source_depth=2),
    ], "default")
    # Mark friend list statuses to ensure depth_min and friend_list_fetched_at
    repo.mark_friend_list_status("1", "public", friend_count=2, friend_count_status="public", friend_ids=["2"], project_id="default")
    repo.mark_friend_list_status("2", "public", friend_count=3, friend_count_status="public", friend_ids=["1", "3", "4"], project_id="default")

    analysis = repo.get_friend_circle_analysis(root="1", max_depth=3, min_mutual=1, limit=5, project_id="default")
    assert len(analysis.candidates) >= 1
    candidate_ids = [c.steam_id for c in analysis.candidates]
    assert "4" in candidate_ids


def test_kuzu_cypher_injection_prevention(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    users = [
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="injection\\' OR 1=1 OR n.steam_id=\\'", persona_name="Hacker", depth_min=1),
    ]
    repo.upsert_users(users, "default")

    graph = repo.get_graph(root="injection\\' OR 1=1 OR n.steam_id=\\'", depth=1, limit=10, project_id="default")
    assert len(graph.nodes) == 1
    assert graph.nodes[0].id == "injection\\' OR 1=1 OR n.steam_id=\\'"


def test_kuzu_bulk_patch_users(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    users = [
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="2", persona_name="Bob", depth_min=1),
    ]
    repo.upsert_users(users, "default")

    patches = [
        {"steam_id": "1", "note": "Alice's note", "tags": ["CS2"], "category": "friend"},
        {"steam_id": "2", "note": "Bob's note", "tags": ["Dota2"], "category": "colleague"},
    ]
    repo.bulk_patch_users(patches)

    graph = repo.get_graph(root=None, depth=1, limit=10, project_id="default")
    alice = [n for n in graph.nodes if n.id == "1"][0]
    bob = [n for n in graph.nodes if n.id == "2"][0]

    assert alice.note == "Alice's note"
    assert alice.tags == ["CS2"]
    assert alice.category == "friend"

    assert bob.note == "Bob's note"
    assert bob.tags == ["Dota2"]
    assert bob.category == "colleague"
