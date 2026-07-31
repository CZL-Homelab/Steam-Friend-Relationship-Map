from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

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


def test_kuzu_close_releases_database_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "kuzu_db"
    repo = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
    repo.ensure_schema()
    repo.close()

    reopened = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
    try:
        reopened.ensure_schema()
        assert reopened.test_connection()
    finally:
        reopened.close()


def test_kuzu_open_failure_does_not_move_database(tmp_path: Path) -> None:
    db_path = tmp_path / "kuzu_db"
    db_path.mkdir()

    with (
        patch("steam_friend_relationship_map.kuzu_repo.kuzu.Database", side_effect=RuntimeError("Could not set lock on file")) as database,
        patch("shutil.move") as move,
        patch("os.rename") as rename,
    ):
        with pytest.raises(RuntimeError, match="already in use"):
            KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)

    database.assert_called_once()
    move.assert_not_called()
    rename.assert_not_called()
    assert db_path.exists()


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


def test_kuzu_friend_list_cache_round_trip(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.mark_friend_list_status(
        "1",
        "public",
        friend_count=2,
        friend_count_status="public",
        friend_ids=["2", "3"],
        project_id="default",
    )

    assert repo.get_cached_friend_list("1", valid_days=14, project_id="default") == (
        "public",
        ["2", "3"],
    )
    assert repo.get_cached_friend_list("1", valid_days=0, project_id="default") is None


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


def test_kuzu_root_graph_uses_bfs_depth_and_view_filters(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.upsert_users(
        [
            SteamUserRecord(steam_id="root", persona_name="Root", depth_min=0, category="root"),
            SteamUserRecord(steam_id="a", persona_name="Alpha", depth_min=1, category="hide"),
            SteamUserRecord(steam_id="b", persona_name="Beta", depth_min=1, category="hide"),
            SteamUserRecord(steam_id="c", persona_name="Gamma", depth_min=2, category="show"),
            SteamUserRecord(steam_id="d", persona_name="Delta", depth_min=3, category="show"),
            SteamUserRecord(steam_id="other", persona_name="Other", depth_min=1, category="show"),
        ],
        "project-a",
    )
    repo.upsert_relationships(
        [
            FriendEdge(from_id="root", to_id="a", crawl_id="run-1", source_depth=0),
            FriendEdge(from_id="root", to_id="b", crawl_id="run-1", source_depth=0),
            FriendEdge(from_id="a", to_id="c", crawl_id="run-1", source_depth=1),
            FriendEdge(from_id="b", to_id="c", crawl_id="run-1", source_depth=1),
            FriendEdge(from_id="c", to_id="d", crawl_id="run-1", source_depth=2),
            FriendEdge(from_id="root", to_id="other", crawl_id="run-2", source_depth=0),
        ],
        "project-a",
    )
    repo.bulk_patch_users([
        {"steam_id": "root", "category": "root"},
        {"steam_id": "a", "category": "hide"},
        {"steam_id": "b", "category": "hide"},
        {"steam_id": "c", "category": "show"},
        {"steam_id": "d", "category": "show"},
        {"steam_id": "other", "category": "show"},
    ])
    repo.upsert_users(
        [
            SteamUserRecord(steam_id="root-b", persona_name="Root Other", depth_min=0),
            SteamUserRecord(steam_id="foreign", persona_name="Foreign", depth_min=1),
        ],
        "project-b",
    )
    repo.upsert_relationships(
        [FriendEdge(from_id="root-b", to_id="foreign", crawl_id="run-3", source_depth=0)],
        "project-b",
    )

    depth_one = repo.get_graph(root="root", depth=1, limit=20, project_id="project-a")
    assert {node.id for node in depth_one.nodes} == {"root", "a", "b", "other"}
    assert depth_one.traversal_depth_reached == 1
    assert depth_one.depth_incomplete is False

    depth_three = repo.get_graph(root="root", depth=3, limit=20, project_id="project-a")
    assert {node.id for node in depth_three.nodes} == {"root", "a", "b", "c", "d", "other"}
    assert {frozenset((edge.source, edge.target)) for edge in depth_three.edges} == {
        frozenset(("root", "a")),
        frozenset(("root", "b")),
        frozenset(("a", "c")),
        frozenset(("b", "c")),
        frozenset(("c", "d")),
        frozenset(("root", "other")),
    }
    assert depth_three.root_found is True
    assert depth_three.requested_depth == 3
    assert depth_three.traversal_depth_reached == 3
    assert depth_three.depth_incomplete is False

    filtered = repo.get_graph(root="root", depth=3, limit=20, category="show", project_id="project-a")
    assert {node.id for node in filtered.nodes} == {"c", "d", "other"}
    assert {frozenset((edge.source, edge.target)) for edge in filtered.edges} == {frozenset(("c", "d"))}
    assert filtered.traversal_depth_reached == 3
    filtered_nodes = {node.id: node for node in filtered.nodes}
    assert filtered_nodes["c"].root_route_count == 2
    assert filtered_nodes["c"].root_route_total_hops == 4

    project_b = repo.get_graph(root="root-b", depth=3, limit=20, project_id="project-b")
    assert {node.id for node in project_b.nodes} == {"root-b", "foreign"}
    assert project_b.traversal_depth_reached == 1
    assert project_b.depth_incomplete is True


def test_kuzu_root_friend_circle_scores_routes_and_total_hops(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.upsert_users(
        [
            SteamUserRecord(steam_id="root", persona_name="Root", depth_min=0),
            SteamUserRecord(steam_id="a", persona_name="A", depth_min=1),
            SteamUserRecord(steam_id="b", persona_name="B", depth_min=1),
            SteamUserRecord(steam_id="c", persona_name="C", depth_min=1),
        ],
        "default",
    )
    repo.upsert_relationships(
        [
            FriendEdge(from_id="root", to_id="a", crawl_id="run-1", source_depth=0),
            FriendEdge(from_id="root", to_id="b", crawl_id="run-1", source_depth=0),
            FriendEdge(from_id="root", to_id="c", crawl_id="run-1", source_depth=0),
            FriendEdge(from_id="a", to_id="b", crawl_id="run-1", source_depth=1),
            FriendEdge(from_id="a", to_id="c", crawl_id="run-1", source_depth=1),
        ],
        "default",
    )

    graph = repo.get_graph(root="root", depth=3, limit=10, project_id="default")
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["root"].root_friend_circle_score == 1_000_000
    assert nodes["a"].root_route_count == 3
    assert nodes["a"].root_route_total_hops == 5
    assert nodes["c"].root_route_count == 3
    assert nodes["c"].root_route_total_hops == 6
    assert nodes["a"].root_friend_circle_score > nodes["c"].root_friend_circle_score

    shallower_graph = repo.get_graph(root="root", depth=2, limit=10, project_id="default")
    shallower_nodes = {node.id: node for node in shallower_graph.nodes}
    assert shallower_nodes["c"].root_route_count == 2
    assert shallower_nodes["c"].root_route_total_hops == 3


def test_kuzu_root_friend_circle_route_cap_is_stable(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    middle_users = [
        SteamUserRecord(steam_id=f"m-{index}", persona_name=f"Middle {index}", depth_min=1)
        for index in range(250)
    ]
    repo.upsert_users(
        [
            SteamUserRecord(steam_id="root", persona_name="Root", depth_min=0),
            SteamUserRecord(steam_id="target", persona_name="Target", depth_min=2),
            *middle_users,
        ],
        "default",
    )
    repo.upsert_relationships(
        [
            edge
            for index in range(250)
            for edge in (
                FriendEdge(from_id="root", to_id=f"m-{index}", crawl_id="run-1", source_depth=0),
                FriendEdge(from_id=f"m-{index}", to_id="target", crawl_id="run-1", source_depth=1),
            )
        ],
        "default",
    )

    graph = repo.get_graph(root="root", depth=2, limit=300, project_id="default")
    nodes = {node.id: node for node in graph.nodes}

    assert nodes["target"].root_route_count == 200
    assert nodes["target"].root_route_total_hops == 400


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


def test_kuzu_relationships_are_isolated_by_project(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    users = [
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="2", persona_name="Bob", depth_min=1),
    ]
    edge = FriendEdge(from_id="1", to_id="2", crawl_id="run-1", source_depth=0)

    repo.upsert_users(users, "project-a")
    repo.upsert_relationships([edge], "project-a")
    assert len(repo.get_graph(root="1", depth=1, limit=10, project_id="project-a").edges) == 1
    assert len(repo.get_graph(root="1", depth=1, limit=10, project_id="project-b").edges) == 0

    repo.upsert_users(users, "project-b")
    repo.upsert_relationships([edge], "project-b")
    assert len(repo.get_graph(root="1", depth=1, limit=10, project_id="project-a").edges) == 1
    assert len(repo.get_graph(root="1", depth=1, limit=10, project_id="project-b").edges) == 1


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
