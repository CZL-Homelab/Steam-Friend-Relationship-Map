from __future__ import annotations

import ast
import shutil
import tempfile
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from steam_friend_relationship_map.kuzu_repo import KuzuRepositoryImpl, _execute_discard
from steam_friend_relationship_map.models import (
    CrawlRun,
    CrawlStatus,
    FriendEdge,
    FriendListCacheUpdate,
    ProjectCreate,
    SteamUserRecord,
)


class CountingConnection:
    def __init__(self, connection, fail_unwind_at: int | None = None) -> None:
        self.connection = connection
        self.queries: list[str] = []
        self.fail_unwind_at = fail_unwind_at
        self.unwind_count = 0

    def execute(self, query: str, parameters=None):
        self.queries.append(query)
        if "UNWIND $rows AS row" in query:
            self.unwind_count += 1
            if self.unwind_count == self.fail_unwind_at:
                raise RuntimeError("synthetic batch failure")
        if parameters is None:
            return self.connection.execute(query)
        return self.connection.execute(query, parameters)


@pytest.fixture
def temp_kuzu_repo() -> Generator[KuzuRepositoryImpl, None, None]:
    db_dir = tempfile.mkdtemp()
    db_path = Path(db_dir) / "kuzu_db"
    repo = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
    try:
        yield repo
    finally:
        repo.close()
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


def test_kuzu_close_releases_connections_created_by_worker_threads(
    tmp_path: Path,
) -> None:
    class FakeDatabase:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    database = FakeDatabase()
    barrier = threading.Barrier(3)

    def use_connection(repo: KuzuRepositoryImpl) -> FakeConnection:
        connection = repo._get_conn()
        barrier.wait(timeout=2)
        return connection  # type: ignore[return-value]

    with (
        patch(
            "steam_friend_relationship_map.kuzu_repo.kuzu.Database",
            return_value=database,
        ),
        patch(
            "steam_friend_relationship_map.kuzu_repo.kuzu.Connection",
            side_effect=lambda _database: FakeConnection(),
        ),
    ):
        repo = KuzuRepositoryImpl(db_path=str(tmp_path / "fake_db"))
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(use_connection, repo) for _ in range(2)]
            barrier.wait(timeout=2)
            connections = [future.result(timeout=2) for future in futures]
        repo.close()

    assert connections[0] is not connections[1]
    assert all(connection.closed for connection in connections)
    assert database.closed is True
    with pytest.raises(RuntimeError, match="closed"):
        repo._get_conn()


def test_kuzu_worker_connection_does_not_keep_real_database_locked(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "worker_kuzu_db"
    repo = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        executor.submit(repo.ensure_schema).result(timeout=5)
        assert executor.submit(repo.test_connection).result(timeout=5)
        repo.close()

        reopened = KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)
        try:
            reopened.ensure_schema()
            assert reopened.test_connection()
        finally:
            reopened.close()
    finally:
        executor.shutdown(wait=True)


def test_kuzu_open_failure_does_not_move_database(tmp_path: Path) -> None:
    db_path = tmp_path / "kuzu_db"
    db_path.mkdir()

    with (
        patch(
            "steam_friend_relationship_map.kuzu_repo.kuzu.Database",
            side_effect=RuntimeError("Could not set lock on file"),
        ) as database,
        patch("shutil.move") as move,
        patch("os.rename") as rename,
    ):
        with pytest.raises(RuntimeError, match="already in use"):
            KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)

    database.assert_called_once()
    move.assert_not_called()
    rename.assert_not_called()
    assert db_path.exists()


def test_kuzu_open_failure_identifies_legacy_recovery_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "kuzu_db"
    wal_path = tmp_path / "kuzu_db.wal"
    backup_path = tmp_path / "kuzu_db_corrupted_20260701_233820"
    db_path.write_bytes(b"db")
    wal_path.write_bytes(b"wal")
    backup_path.write_bytes(b"recovered database")

    with patch(
        "steam_friend_relationship_map.kuzu_repo.kuzu.Database",
        side_effect=IndexError("invalid unordered_map<K, T> key"),
    ) as database:
        with pytest.raises(RuntimeError) as error:
            KuzuRepositoryImpl(db_path=str(db_path), buffer_pool_size_gb=1)

    message = str(error.value)
    assert "storage files appear to be inconsistent" in message
    assert str(backup_path) in message
    assert "do not delete or overwrite the originals" in message
    database.assert_called_once()
    assert db_path.read_bytes() == b"db"
    assert wal_path.read_bytes() == b"wal"
    assert backup_path.read_bytes() == b"recovered database"


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


def test_kuzu_rejects_dynamic_crawl_run_field_names(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    with pytest.raises(ValueError, match="Unsupported crawl run update fields"):
        temp_kuzu_repo.update_crawl_run("run-1", **{"status = 'failed'": "ignored"})


def test_kuzu_recovers_crawls_interrupted_by_restart(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    statuses = [
        CrawlStatus.pending,
        CrawlStatus.running,
        CrawlStatus.paused,
        CrawlStatus.completed,
    ]
    for index, status in enumerate(statuses):
        repo.start_crawl_run(
            CrawlRun(
                id=f"recovery-{index}",
                root_steam_id="root",
                max_depth=2,
                max_nodes=100,
                status=status,
            ),
            "default",
        )

    assert repo.recover_interrupted_crawls() == 3
    for index in range(3):
        recovered = repo.get_crawl_run(f"recovery-{index}")
        assert recovered is not None
        assert recovered.status == CrawlStatus.stopped
        assert recovered.finished_at
        assert recovered.message == "应用重启前抓取未正常结束"
        assert recovered.last_event == recovered.message

    completed = repo.get_crawl_run("recovery-3")
    assert completed is not None
    assert completed.status == CrawlStatus.completed
    assert repo.recover_interrupted_crawls() == 0


def test_kuzu_friend_list_cache_round_trip(
    temp_kuzu_repo: KuzuRepositoryImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    repo.mark_friend_list_status(
        "2",
        "private",
        friend_count=None,
        friend_count_status="private",
        friend_ids=["stale"],
        project_id="default",
    )
    repo.mark_friend_list_status(
        "3",
        "unknown",
        friend_count=None,
        friend_count_status="unknown",
        friend_ids=[],
        project_id="default",
    )
    repo.mark_friend_list_status(
        "4",
        "public",
        friend_count=None,
        friend_count_status="public",
        friend_ids=[],
        project_id="default",
    )
    connection = repo._get_conn()
    connection.execute("MATCH (u:SteamUser {steam_id: '4'}) SET u.friend_ids = NULL")

    assert repo.get_cached_friend_list("1", valid_days=14, project_id="default") == (
        "public",
        ["2", "3"],
    )
    assert repo.get_cached_friend_list("1", valid_days=0, project_id="default") is None

    counting = CountingConnection(connection)
    monkeypatch.setattr(repo, "_get_conn", lambda: counting)
    cached = repo.get_cached_friend_lists(
        ["1", "2", "3", "4", "missing", "1"],
        valid_days=14,
        project_id="default",
    )
    assert cached == {"1": ("public", ["2", "3"]), "2": ("private", [])}
    assert sum("u.steam_id IN $steam_ids" in query for query in counting.queries) == 1

    counting.queries.clear()
    assert repo.get_cached_friend_lists(["1", "2"], valid_days=0, project_id="default") == {}
    assert counting.queries == []


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
    repo.upsert_relationships(
        [
            FriendEdge(from_id="2", to_id="4", crawl_id="run-1", source_depth=1),
            FriendEdge(from_id="3", to_id="4", crawl_id="run-1", source_depth=2),
        ],
        "default",
    )
    # Mark friend list statuses to ensure depth_min and friend_list_fetched_at
    repo.mark_friend_list_status(
        "1",
        "public",
        friend_count=2,
        friend_count_status="public",
        friend_ids=["2"],
        project_id="default",
    )
    repo.mark_friend_list_status(
        "2",
        "public",
        friend_count=3,
        friend_count_status="public",
        friend_ids=["1", "3", "4"],
        project_id="default",
    )

    analysis = repo.get_friend_circle_analysis(
        root="1", max_depth=3, min_mutual=1, limit=5, project_id="default"
    )
    assert len(analysis.candidates) >= 1
    candidate_ids = [c.steam_id for c in analysis.candidates]
    assert "4" in candidate_ids


def test_kuzu_path_and_friend_circle_use_bounded_bfs(
    temp_kuzu_repo: KuzuRepositoryImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.upsert_users(
        [
            SteamUserRecord(steam_id="root", persona_name="Root", depth_min=0),
            SteamUserRecord(steam_id="a", persona_name="A", depth_min=1),
            SteamUserRecord(steam_id="b", persona_name="B", depth_min=1),
            SteamUserRecord(steam_id="c", persona_name="C", depth_min=2),
            SteamUserRecord(steam_id="d", persona_name="D", depth_min=2),
        ],
        "default",
    )
    repo.upsert_relationships(
        [
            FriendEdge(from_id="root", to_id="a", crawl_id="run", source_depth=0),
            FriendEdge(from_id="root", to_id="b", crawl_id="run", source_depth=0),
            FriendEdge(from_id="a", to_id="c", crawl_id="run", source_depth=1),
            FriendEdge(from_id="b", to_id="c", crawl_id="run", source_depth=1),
            FriendEdge(from_id="a", to_id="d", crawl_id="run", source_depth=1),
            FriendEdge(from_id="c", to_id="d", crawl_id="run", source_depth=2),
        ],
        "default",
    )

    connection = repo._get_conn()
    counting = CountingConnection(connection)
    monkeypatch.setattr(repo, "_get_conn", lambda: counting)

    assert repo.get_shortest_path("root", "d", 1, "default").nodes == []
    path = repo.get_shortest_path("root", "d", 2, "default")
    assert [node.id for node in path.nodes] == ["root", "a", "d"]
    assert [edge.source for edge in path.edges] == ["root", "a"]
    assert [node.id for node in repo.get_shortest_path("root", "root", 0, "default").nodes] == [
        "root"
    ]

    analysis = repo.get_friend_circle_analysis(
        root="root",
        max_depth=2,
        min_mutual=2,
        limit=10,
        project_id="default",
    )
    assert [candidate.steam_id for candidate in analysis.candidates] == ["c"]
    assert analysis.candidates[0].depth == 2
    assert analysis.candidates[0].mutual_count == 2
    assert [node.id for node in analysis.candidates[0].evidence] == ["a", "b"]

    normalized_queries = [" ".join(query.split()) for query in counting.queries]
    assert all("MATCH p=" not in query for query in normalized_queries)
    assert all(":STEAM_FRIEND*" not in query for query in normalized_queries)


def test_kuzu_batches_large_graph_writes_and_deduplicates_relationships(
    temp_kuzu_repo: KuzuRepositoryImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    connection = repo._get_conn()
    counting = CountingConnection(connection)
    monkeypatch.setattr(repo, "_get_conn", lambda: counting)

    steam_ids = [f"batch-{index:04d}" for index in range(1002)]
    repo.upsert_users(
        [
            SteamUserRecord(steam_id=steam_id, depth_min=index % 4)
            for index, steam_id in enumerate(steam_ids)
        ],
        "batch-project",
    )

    user_batch_queries = [query for query in counting.queries if "UNWIND $rows AS row" in query]
    assert len(user_batch_queries) == 3
    user_count = connection.execute(
        """
        MATCH (u:SteamUser)-[:IN_PROJECT]->(p:Project)
        WHERE p.id = $project_id
        RETURN count(u)
        """,
        {"project_id": "batch-project"},
    ).get_next()[0]
    assert user_count == 1002

    counting.queries.clear()
    edges = [
        FriendEdge(
            from_id=steam_ids[index],
            to_id=steam_ids[index + 1],
            crawl_id="batch-crawl",
            source_depth=index % 4,
        )
        for index in range(1001)
    ]
    edges.extend(
        [
            FriendEdge(
                from_id=steam_ids[1],
                to_id=steam_ids[0],
                crawl_id="duplicate",
                source_depth=9,
            ),
            FriendEdge(
                from_id=steam_ids[0],
                to_id=steam_ids[0],
                crawl_id="self-loop",
                source_depth=0,
            ),
        ]
    )
    repo.upsert_relationships(edges, "batch-project")

    relationship_batch_queries = [
        query for query in counting.queries if "UNWIND $rows AS row" in query
    ]
    assert len(relationship_batch_queries) == 3
    relationship_count = connection.execute(
        """
        MATCH ()-[r:STEAM_FRIEND]->()
        WHERE r.project_id = $project_id
        RETURN count(r)
        """,
        {"project_id": "batch-project"},
    ).get_next()[0]
    assert relationship_count == 1001

    counting.queries.clear()
    repo.bulk_patch_users(
        [
            {
                "steam_id": steam_id,
                "note": "batched",
                "tags": [],
                "category": "load-test",
            }
            for steam_id in steam_ids
        ],
        project_id="batch-project",
    )
    patch_batch_queries = [query for query in counting.queries if "UNWIND $rows AS row" in query]
    assert len(patch_batch_queries) == 3
    patched_count = connection.execute(
        """
        MATCH (:SteamUser)-[membership:IN_PROJECT]->(p:Project)
        WHERE p.id = $project_id AND membership.note = 'batched'
        RETURN count(membership)
        """,
        {"project_id": "batch-project"},
    ).get_next()[0]
    assert patched_count == 1002

    counting.queries.clear()
    repo.mark_friend_list_statuses(
        [
            FriendListCacheUpdate(
                steam_id=steam_id,
                status="private",
                friend_count=None,
                friend_count_status="private",
                friend_ids=[],
            )
            for steam_id in steam_ids
        ],
        project_id="batch-project",
    )
    cache_write_queries = [query for query in counting.queries if "UNWIND $rows AS row" in query]
    assert len(cache_write_queries) == 3
    private_count = connection.execute(
        """
        MATCH (u:SteamUser)-[:IN_PROJECT]->(p:Project)
        WHERE p.id = $project_id AND u.friend_list_status = 'private'
        RETURN count(u)
        """,
        {"project_id": "batch-project"},
    ).get_next()[0]
    assert private_count == 1002


def test_kuzu_batched_user_write_rolls_back_all_chunks(
    temp_kuzu_repo: KuzuRepositoryImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    connection = repo._get_conn()
    failing = CountingConnection(connection, fail_unwind_at=2)
    monkeypatch.setattr(repo, "_get_conn", lambda: failing)

    with pytest.raises(RuntimeError, match="synthetic batch failure"):
        repo.upsert_users(
            [SteamUserRecord(steam_id=f"rollback-{index:04d}") for index in range(501)],
            "rollback-project",
        )

    result = connection.execute(
        """
        MATCH (u:SteamUser)-[:IN_PROJECT]->(p:Project)
        WHERE p.id = $project_id
        RETURN count(u)
        """,
        {"project_id": "rollback-project"},
    )
    assert result.get_next()[0] == 0


def test_kuzu_root_graph_uses_bfs_depth_and_view_filters(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
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
    repo.bulk_patch_users(
        [
            {"steam_id": "root", "category": "root"},
            {"steam_id": "a", "category": "hide"},
            {"steam_id": "b", "category": "hide"},
            {"steam_id": "c", "category": "show"},
            {"steam_id": "d", "category": "show"},
            {"steam_id": "other", "category": "show"},
        ],
        project_id="project-a",
    )
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
    assert {node.id for node in depth_three.nodes} == {
        "root",
        "a",
        "b",
        "c",
        "d",
        "other",
    }
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

    filtered = repo.get_graph(
        root="root", depth=3, limit=20, category="show", project_id="project-a"
    )
    assert {node.id for node in filtered.nodes} == {"c", "d", "other"}
    assert {frozenset((edge.source, edge.target)) for edge in filtered.edges} == {
        frozenset(("c", "d"))
    }
    assert filtered.traversal_depth_reached == 3
    filtered_nodes = {node.id: node for node in filtered.nodes}
    assert filtered_nodes["c"].root_route_count == 2
    assert filtered_nodes["c"].root_route_total_hops == 4

    project_b = repo.get_graph(root="root-b", depth=3, limit=20, project_id="project-b")
    assert {node.id for node in project_b.nodes} == {"root-b", "foreign"}
    assert project_b.traversal_depth_reached == 1
    assert project_b.depth_incomplete is True


def test_kuzu_root_friend_circle_scores_routes_and_total_hops(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
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


def test_kuzu_root_friend_circle_route_cap_is_stable(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
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
                FriendEdge(
                    from_id=f"m-{index}",
                    to_id="target",
                    crawl_id="run-1",
                    source_depth=1,
                ),
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
        SteamUserRecord(
            steam_id="injection\\' OR 1=1 OR n.steam_id=\\'",
            persona_name="Hacker",
            depth_min=1,
        ),
    ]
    repo.upsert_users(users, "default")

    graph = repo.get_graph(
        root="injection\\' OR 1=1 OR n.steam_id=\\'",
        depth=1,
        limit=10,
        project_id="default",
    )
    assert len(graph.nodes) == 1
    assert graph.nodes[0].id == "injection\\' OR 1=1 OR n.steam_id=\\'"


def test_kuzu_relationships_are_isolated_by_project(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
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


def test_kuzu_shared_users_and_isolated_members_survive_other_project_deletion(
    temp_kuzu_repo: KuzuRepositoryImpl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.create_project(ProjectCreate(name="Project A"), project_id="project-a")
    repo.create_project(ProjectCreate(name="Project B"), project_id="project-b")

    repo.upsert_users(
        [
            SteamUserRecord(steam_id="shared", persona_name="Shared"),
            SteamUserRecord(steam_id="a-only", persona_name="A Only"),
            SteamUserRecord(steam_id="a-isolated", persona_name="A Isolated"),
        ],
        "project-a",
    )
    repo.upsert_relationships(
        [FriendEdge(from_id="shared", to_id="a-only", crawl_id="a-run", source_depth=0)],
        "project-a",
    )
    repo.upsert_users(
        [
            SteamUserRecord(steam_id="shared", persona_name="Shared"),
            SteamUserRecord(steam_id="b-only", persona_name="B Only"),
            SteamUserRecord(steam_id="b-isolated", persona_name="B Isolated"),
        ],
        "project-b",
    )
    repo.upsert_relationships(
        [FriendEdge(from_id="shared", to_id="b-only", crawl_id="b-run", source_depth=0)],
        "project-b",
    )
    repo.start_crawl_run(
        CrawlRun(
            id="a-run",
            root_steam_id="shared",
            max_depth=1,
            max_nodes=10,
            status=CrawlStatus.completed,
        ),
        "project-a",
    )
    repo.start_crawl_run(
        CrawlRun(
            id="b-run",
            root_steam_id="shared",
            max_depth=1,
            max_nodes=10,
            status=CrawlStatus.completed,
        ),
        "project-b",
    )

    project_a = repo.get_graph(root=None, depth=1, limit=20, project_id="project-a")
    project_b = repo.get_graph(root=None, depth=1, limit=20, project_id="project-b")
    assert {node.id for node in project_a.nodes} == {"shared", "a-only", "a-isolated"}
    assert {node.id for node in project_b.nodes} == {"shared", "b-only", "b-isolated"}
    assert repo.get_db_stats("project-a").steam_users == 3
    assert repo.get_db_stats("project-b").steam_users == 3
    connection = repo._get_conn()
    counting = CountingConnection(connection)
    monkeypatch.setattr(repo, "_get_conn", lambda: counting)
    projects = {project.id: project for project in repo.list_projects().projects}
    assert len(counting.queries) == 4
    assert (
        projects["project-a"].steam_users,
        projects["project-a"].relationships,
        projects["project-a"].crawl_runs,
    ) == (3, 1, 1)
    assert (
        projects["project-b"].steam_users,
        projects["project-b"].relationships,
        projects["project-b"].crawl_runs,
    ) == (3, 1, 1)
    assert {node["steam_id"] for node in repo.export_graph("project-a").nodes} == {
        "shared",
        "a-only",
        "a-isolated",
    }

    assert repo.delete_project("project-a")

    remaining = repo.get_graph(root=None, depth=1, limit=20, project_id="project-b")
    assert {node.id for node in remaining.nodes} == {"shared", "b-only", "b-isolated"}
    assert {(edge.source, edge.target) for edge in remaining.edges} == {("b-only", "shared")}
    assert repo.get_db_stats("project-b").steam_users == 3
    assert not repo.project_exists("project-a")


def test_kuzu_project_membership_migration_backfills_legacy_relationships(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.create_project(ProjectCreate(name="Project A"), project_id="project-a")
    repo.create_project(ProjectCreate(name="Project B"), project_id="project-b")
    users = [
        SteamUserRecord(steam_id="shared", persona_name="Shared"),
        SteamUserRecord(steam_id="neighbor", persona_name="Neighbor"),
    ]
    edge = FriendEdge(from_id="shared", to_id="neighbor", crawl_id="run", source_depth=0)
    repo.upsert_users(users, "project-a")
    repo.upsert_relationships([edge], "project-b")

    conn = repo._get_conn()
    conn.execute("MATCH (:SteamUser)-[m:IN_PROJECT]->(:Project) DELETE m")
    conn.execute("MATCH (m:SchemaMigration) WHERE m.id = 'project-membership-v1' DELETE m")
    conn.execute("MATCH (p:Project) WHERE p.id IN ['project-a', 'project-b'] DELETE p")
    conn.execute("DROP TABLE IN_PROJECT")
    conn.execute("DROP TABLE SchemaMigration")

    repo.ensure_schema()

    assert repo.project_exists("project-a")
    assert repo.project_exists("project-b")
    assert {
        node.id
        for node in repo.get_graph(root=None, depth=1, limit=10, project_id="project-a").nodes
    } == {
        "shared",
        "neighbor",
    }
    assert {
        node.id
        for node in repo.get_graph(root=None, depth=1, limit=10, project_id="project-b").nodes
    } == {
        "shared",
        "neighbor",
    }


def test_kuzu_legacy_default_edges_do_not_leak_into_other_projects(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    users = [
        SteamUserRecord(steam_id="1", persona_name="One"),
        SteamUserRecord(steam_id="2", persona_name="Two"),
    ]
    edge = FriendEdge(from_id="1", to_id="2", crawl_id="legacy", source_depth=0)
    repo.upsert_users(users, "default")
    repo.upsert_relationships([edge], "default")
    repo.upsert_users(users, "project-b")

    repo._get_conn().execute(
        "MATCH ()-[r:STEAM_FRIEND]->() WHERE r.project_id = 'default' SET r.project_id = ''"
    )

    assert len(repo.get_graph(root="1", depth=1, limit=10, project_id="default").edges) == 1
    assert len(repo.get_graph(root="1", depth=1, limit=10, project_id="project-b").edges) == 0
    assert len(repo.export_graph("default").edges) == 1
    assert len(repo.export_graph("project-b").edges) == 0


def test_kuzu_bulk_patch_users(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    users = [
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="2", persona_name="Bob", depth_min=1),
    ]
    repo.upsert_users(users, "default")

    patches = [
        {
            "steam_id": "1",
            "note": "Alice's note",
            "tags": ["CS2"],
            "category": "friend",
        },
        {
            "steam_id": "2",
            "note": "Bob's note",
            "tags": ["Dota2"],
            "category": "colleague",
        },
        {"steam_id": "1", "tags": [], "category": "close friend"},
    ]
    repo.bulk_patch_users(patches)

    graph = repo.get_graph(root=None, depth=1, limit=10, project_id="default")
    alice = [n for n in graph.nodes if n.id == "1"][0]
    bob = [n for n in graph.nodes if n.id == "2"][0]

    assert alice.note == "Alice's note"
    assert alice.tags == []
    assert alice.category == "close friend"

    assert bob.note == "Bob's note"
    assert bob.tags == ["Dota2"]
    assert bob.category == "colleague"


def test_kuzu_project_member_metadata_is_fully_isolated(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.upsert_users(
        [
            SteamUserRecord(
                steam_id="shared",
                persona_name="Shared",
                depth_min=1,
                prior_pool_link_count=2,
                root_closeness_score=10.0,
                last_scored_crawl_id="crawl-a",
            )
        ],
        "project-a",
    )
    repo.patch_user(
        "shared",
        note="Project A note",
        tags=["alpha"],
        category="a-category",
        project_id="project-a",
    )
    repo.upsert_users(
        [
            SteamUserRecord(
                steam_id="shared",
                persona_name="Shared updated",
                depth_min=3,
                prior_pool_link_count=7,
                root_closeness_score=4.0,
                last_scored_crawl_id="crawl-b",
            )
        ],
        "project-b",
    )
    repo.patch_user(
        "shared",
        note="Project B note",
        tags=["beta"],
        category="b-category",
        project_id="project-b",
    )

    project_a = repo.get_graph(root=None, depth=1, limit=10, project_id="project-a")
    project_b = repo.get_graph(root=None, depth=1, limit=10, project_id="project-b")
    node_a = project_a.nodes[0]
    node_b = project_b.nodes[0]

    assert node_a.label == "Shared updated"
    assert (
        node_a.depth,
        node_a.prior_pool_link_count,
        node_a.root_closeness_score,
    ) == (1, 2, 10.0)
    assert (node_a.note, node_a.tags, node_a.category) == (
        "Project A note",
        ["alpha"],
        "a-category",
    )
    assert (
        node_b.depth,
        node_b.prior_pool_link_count,
        node_b.root_closeness_score,
    ) == (3, 7, 4.0)
    assert (node_b.note, node_b.tags, node_b.category) == (
        "Project B note",
        ["beta"],
        "b-category",
    )

    assert (
        len(
            repo.get_graph(
                root=None,
                depth=1,
                limit=10,
                category="a-category",
                project_id="project-a",
            ).nodes
        )
        == 1
    )
    assert not repo.get_graph(
        root=None, depth=1, limit=10, category="a-category", project_id="project-b"
    ).nodes

    exported_a = repo.export_graph("project-a").nodes[0]
    exported_b = repo.export_graph("project-b").nodes[0]
    assert exported_a["project_id"] == "project-a"
    assert exported_a["note"] == "Project A note"
    assert exported_a["depth_min"] == 1
    assert exported_a["last_scored_crawl_id"] == "crawl-a"
    assert exported_b["project_id"] == "project-b"
    assert exported_b["note"] == "Project B note"
    assert exported_b["depth_min"] == 3
    assert exported_b["last_scored_crawl_id"] == "crawl-b"


def test_kuzu_export_closes_consumed_query_results() -> None:
    class ClosableResult:
        def __init__(self, rows: list[list[object]]) -> None:
            self.rows = rows
            self.index = 0
            self.closed = False

        def has_next(self) -> bool:
            return self.index < len(self.rows)

        def get_next(self) -> list[object]:
            row = self.rows[self.index]
            self.index += 1
            return row

        def close(self) -> None:
            self.closed = True

    class ExportConnection:
        def __init__(self) -> None:
            self.results = [
                ClosableResult([[{"steam_id": "a", "persona_name": "A"}, {"depth_min": 1}]]),
                ClosableResult([["a", "b"]]),
            ]

        def execute(self, _query: str, _parameters: object) -> ClosableResult:
            return self.results.pop(0)

    connection = ExportConnection()
    node_result, edge_result = connection.results
    repo = object.__new__(KuzuRepositoryImpl)
    repo._get_conn = lambda: connection  # type: ignore[method-assign]

    exported = repo.export_graph("project-a")

    assert exported.nodes[0]["steam_id"] == "a"
    assert exported.edges == [{"source": "a", "target": "b"}]
    assert node_result.closed is True
    assert edge_result.closed is True


def test_kuzu_results_are_iterated_only_by_closing_consumer() -> None:
    source = Path("src/steam_friend_relationship_map/kuzu_repo.py").read_text(encoding="utf-8")

    assert source.count(".has_next()") == 1
    assert "while result.has_next():" in source
    assert "return list(_iter_rows(result))" in source

    tree = ast.parse(source)
    bare_execute_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "execute"
    ]
    assert bare_execute_lines == []


def test_kuzu_discarded_results_are_closed() -> None:
    class ClosableResult:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class RecordingConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.results: list[ClosableResult] = []

        def execute(self, query: str, parameters: object = None) -> ClosableResult:
            self.calls.append((query, parameters))
            result = ClosableResult()
            self.results.append(result)
            return result

    connection = RecordingConnection()

    _execute_discard(connection, "BEGIN TRANSACTION")  # type: ignore[arg-type]
    _execute_discard(connection, "SET value = $value", {"value": 1})  # type: ignore[arg-type]

    assert connection.calls == [
        ("BEGIN TRANSACTION", None),
        ("SET value = $value", {"value": 1}),
    ]
    assert all(result.closed for result in connection.results)


def test_kuzu_project_member_metadata_migration_copies_legacy_primary_project(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.upsert_users(
        [SteamUserRecord(steam_id="legacy", persona_name="Legacy")],
        "project-a",
    )
    conn = repo._get_conn()
    conn.execute(
        """
        MATCH (u:SteamUser {steam_id: 'legacy'})
        SET u.depth_min = 2,
            u.prior_pool_link_count = 5,
            u.root_closeness_score = 8.5,
            u.last_scored_crawl_id = 'legacy-crawl',
            u.note = 'legacy note',
            u.tags = ['legacy-tag'],
            u.category = 'legacy-category'
        """
    )
    conn.execute("MATCH (:SteamUser)-[membership:IN_PROJECT]->(:Project) DELETE membership")
    conn.execute("DROP TABLE IN_PROJECT")
    conn.execute("CREATE REL TABLE IN_PROJECT(FROM SteamUser TO Project)")
    conn.execute(
        """
        MATCH (u:SteamUser {steam_id: 'legacy'})
        MATCH (p:Project {id: 'project-a'})
        CREATE (u)-[:IN_PROJECT]->(p)
        """
    )
    conn.execute(
        "MATCH (migration:SchemaMigration) WHERE migration.id = 'project-member-metadata-v2' DELETE migration"
    )

    repo.ensure_schema()

    node = repo.get_graph(root=None, depth=1, limit=10, project_id="project-a").nodes[0]
    assert (node.depth, node.prior_pool_link_count, node.root_closeness_score) == (
        2,
        5,
        8.5,
    )
    assert (node.note, node.tags, node.category) == (
        "legacy note",
        ["legacy-tag"],
        "legacy-category",
    )
