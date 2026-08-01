from __future__ import annotations

import re
from typing import Any

from steam_friend_relationship_map.models import (
    FriendEdge,
    FriendListCacheUpdate,
    SteamUserRecord,
)
from steam_friend_relationship_map.neo4j_repo import Neo4jRepositoryImpl


class _FakeResult:
    def __init__(
        self, query: str, params: dict[str, Any], driver: "_FakeDriver"
    ) -> None:
        self.query = query
        self.params = params
        self.driver = driver

    def consume(self) -> None:
        return None

    def single(self) -> dict[str, Any] | None:
        if "RETURN m.id AS id" in self.query:
            migration_id = str(self.params["id"])
            return (
                {"id": migration_id} if migration_id in self.driver.migrations else None
            )
        if "RETURN p" in self.query:
            return {"p": {"id": "project-a"}}
        if "c.status IN $statuses" in self.query and "AS count" in self.query:
            return {"count": self.driver.recovery_count}
        if "AS count" in self.query:
            return {"count": 0}
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        if "u.steam_id AS steam_id" in self.query:
            return iter(self.driver.cache_records)
        if "RETURN p.id AS id" in self.query:
            return iter(self.driver.project_records)
        if "AS user_count" in self.query:
            return iter(self.driver.project_user_counts)
        if "AS relationship_count" in self.query:
            return iter(self.driver.project_relationship_counts)
        if "AS crawl_count" in self.query:
            return iter(self.driver.project_crawl_counts)
        return iter(())


class _FakeSession:
    def __init__(self, driver: "_FakeDriver") -> None:
        self.driver = driver
        self.in_write_transaction = False

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run(self, query: str, **params: Any) -> _FakeResult:
        placeholders = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", query))
        assert placeholders <= set(params), (
            f"Missing Cypher params {placeholders - set(params)} for query: {query}"
        )
        self.driver.queries.append(query)
        self.driver.query_params.append(params)
        if self.in_write_transaction:
            self.driver.transaction_queries.append(query)
        if "MERGE (m:SchemaMigration" in query:
            self.driver.migrations.add(str(params["id"]))
        return _FakeResult(query, params, self.driver)

    def execute_write(self, work):  # type: ignore[no-untyped-def]
        self.driver.execute_write_calls += 1
        self.in_write_transaction = True
        try:
            return work(self)
        finally:
            self.in_write_transaction = False


class _FakeDriver:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.query_params: list[dict[str, Any]] = []
        self.cache_records: list[dict[str, Any]] = []
        self.project_records: list[dict[str, Any]] = []
        self.project_user_counts: list[dict[str, Any]] = []
        self.project_relationship_counts: list[dict[str, Any]] = []
        self.project_crawl_counts: list[dict[str, Any]] = []
        self.recovery_count = 0
        self.migrations: set[str] = set()
        self.execute_write_calls = 0
        self.transaction_queries: list[str] = []

    def session(self) -> _FakeSession:
        return _FakeSession(self)

    def close(self) -> None:
        return None


def _repo() -> tuple[Neo4jRepositoryImpl, _FakeDriver]:
    driver = _FakeDriver()
    repo = Neo4jRepositoryImpl.__new__(Neo4jRepositoryImpl)
    repo.driver = driver  # type: ignore[assignment]
    return repo, driver


def test_neo4j_project_scoped_queries_bind_all_parameters() -> None:
    repo, driver = _repo()
    users = [
        SteamUserRecord(steam_id="1", persona_name="One"),
        SteamUserRecord(steam_id="2", persona_name="Two"),
    ]
    edge = FriendEdge(from_id="1", to_id="2", crawl_id="run", source_depth=0)

    repo.ensure_schema()
    repo.upsert_users(users, "project-a")
    repo.mark_friend_list_status(
        "1",
        "public",
        friend_count=1,
        friend_count_status="public",
        friend_ids=["2"],
        project_id="project-a",
    )
    repo.upsert_relationships([edge], "project-a")
    repo.patch_user("1", note="one", category="friend", project_id="project-a")
    repo.bulk_patch_users([{"steam_id": "2", "tags": ["two"]}], project_id="project-a")
    repo.count_inner_layer_links(["2"], ["1"], "project-a")
    repo.get_graph(root=None, depth=2, limit=20, project_id="project-a")
    repo.get_graph(root="1", depth=2, limit=20, project_id="project-a")
    repo.get_shortest_path("1", "2", 4, "project-a")
    repo.get_friend_circle_analysis("1", project_id="project-a")
    repo.get_top_degree(project_id="project-a")
    repo.get_db_stats("project-a")
    repo.export_graph("project-a")
    assert repo.delete_project("project-a")

    assert driver.migrations == {"project-membership-v1", "project-member-metadata-v2"}
    assert any("MERGE (u)-[:IN_PROJECT]->(p)" in query for query in driver.queries)
    assert any("membership.note" in query for query in driver.queries)
    assert any("coalesce(membership.category" in query for query in driver.queries)
    assert any(
        "NOT EXISTS { MATCH (u)-[:IN_PROJECT]->(:Project) }" in query
        for query in driver.queries
    )
    assert driver.execute_write_calls == 1
    assert len(driver.transaction_queries) == 5
    assert all(
        "Project" in query or "CrawlRun" in query or "STEAM_FRIEND" in query
        for query in driver.transaction_queries
    )


def test_neo4j_project_membership_migration_is_idempotent() -> None:
    repo, driver = _repo()

    repo.ensure_schema()
    first_migration_query_count = sum(
        "MERGE (u)-[:IN_PROJECT]->(p)" in query for query in driver.queries
    )
    repo.ensure_schema()

    assert first_migration_query_count == 1
    assert sum("MERGE (u)-[:IN_PROJECT]->(p)" in query for query in driver.queries) == 1


def test_neo4j_recovers_crawls_interrupted_by_restart() -> None:
    repo, driver = _repo()
    driver.recovery_count = 3

    assert repo.recover_interrupted_crawls() == 3

    query = driver.queries[-1]
    params = driver.query_params[-1]
    assert "c.status IN $statuses" in query
    assert params["statuses"] == ["pending", "running", "paused"]
    assert params["status"] == "stopped"
    assert params["message"] == "应用重启前抓取未正常结束"
    assert params["finished_at"]


def test_neo4j_batches_friend_cache_reads_and_ignores_incomplete_rows() -> None:
    repo, driver = _repo()
    driver.cache_records = [
        {"steam_id": "public", "status": "public", "friend_ids": ["a", "b"]},
        {"steam_id": "private", "status": "private", "friend_ids": ["stale"]},
        {"steam_id": "unknown", "status": "unknown", "friend_ids": []},
        {"steam_id": "legacy", "status": "public", "friend_ids": None},
    ]

    cached = repo.get_cached_friend_lists(
        ["public", "private", "unknown", "legacy", "missing", "public"],
        valid_days=14,
        project_id="project-a",
    )

    assert cached == {
        "public": ("public", ["a", "b"]),
        "private": ("private", []),
    }
    cache_queries = [
        (query, params)
        for query, params in zip(driver.queries, driver.query_params, strict=True)
        if "u.steam_id IN $steam_ids" in query
    ]
    assert len(cache_queries) == 1
    assert cache_queries[0][1]["steam_ids"] == [
        "public",
        "private",
        "unknown",
        "legacy",
        "missing",
    ]

    driver.queries.clear()
    driver.query_params.clear()
    assert repo.get_cached_friend_lists(["public"], 0, "project-a") == {}
    assert driver.queries == []


def test_neo4j_batches_friend_cache_updates() -> None:
    repo, driver = _repo()
    updates = [
        FriendListCacheUpdate(
            steam_id=f"user-{index}",
            status="private",
            friend_count=None,
            friend_count_status="private",
            friend_ids=[],
        )
        for index in range(1001)
    ]

    repo.mark_friend_list_statuses(updates, "project-a")

    batch_queries = [
        (query, params)
        for query, params in zip(driver.queries, driver.query_params, strict=True)
        if "UNWIND $updates AS update" in query
    ]
    assert len(batch_queries) == 2
    assert len(batch_queries[0][1]["updates"]) == 1000
    assert len(batch_queries[1][1]["updates"]) == 1


def test_neo4j_lists_projects_with_fixed_aggregation_queries() -> None:
    repo, driver = _repo()
    driver.project_records = [
        {"id": "project-a", "name": "Project A", "created_at": "2026-01-02T00:00:00Z"},
        {"id": "default", "name": "Default", "created_at": "2026-01-01T00:00:00Z"},
    ]
    driver.project_user_counts = [
        {"project_id": "project-a", "user_count": 3},
        {"project_id": "default", "user_count": 2},
    ]
    driver.project_relationship_counts = [
        {"project_id": "project-a", "relationship_count": 2},
        {"project_id": "default", "relationship_count": 1},
    ]
    driver.project_crawl_counts = [
        {"project_id": "project-a", "crawl_count": 4},
    ]

    projects = {project.id: project for project in repo.list_projects().projects}

    assert len(driver.queries) == 4
    assert all("OPTIONAL MATCH" not in query for query in driver.queries)
    assert any("count(DISTINCT r)" in query for query in driver.queries)
    assert any("THEN 'default'" in query for query in driver.queries)
    assert (
        projects["project-a"].steam_users,
        projects["project-a"].relationships,
        projects["project-a"].crawl_runs,
    ) == (3, 2, 4)
    assert (
        projects["default"].steam_users,
        projects["default"].relationships,
        projects["default"].crawl_runs,
    ) == (2, 1, 0)
