from __future__ import annotations

from pathlib import Path


def test_neo4j_queries_do_not_use_removed_size_pattern() -> None:
    source = Path("src/steam_friend_relationship_map/neo4j_repo.py").read_text(encoding="utf-8")

    assert "size((n)-[:STEAM_FRIEND]-())" not in source
    assert "COUNT {" in source
    assert "(n)-[degree_rel:STEAM_FRIEND]-()" in source


def test_neo4j_relationship_merge_is_project_scoped() -> None:
    source = Path("src/steam_friend_relationship_map/neo4j_repo.py").read_text(encoding="utf-8")

    assert "MERGE (a)-[r:STEAM_FRIEND {project_id: $project_id}]-(b)" in source
    assert "MERGE (a)-[r:STEAM_FRIEND]-(b)" not in source


def test_neo4j_uses_explicit_project_membership_and_safe_deletion() -> None:
    source = Path("src/steam_friend_relationship_map/neo4j_repo.py").read_text(encoding="utf-8")

    assert "MERGE (u)-[:IN_PROJECT]->(p)" in source
    assert "MATCH (n:SteamUser)-[membership:IN_PROJECT]->(:Project {id: $pid})" in source
    assert 'migration_id = "project-member-metadata-v2"' in source
    assert "coalesce(membership.category, '')" in source
    assert "ORDER BY membership.depth_min" in source
    assert "MATCH (u:SteamUser {project_id: $pid})\n                DETACH DELETE u" not in source
    assert "NOT EXISTS { MATCH (u)-[:IN_PROJECT]->(:Project) }" in source
    assert "MATCH ()-[r:STEAM_FRIEND {project_id: $pid}]-()" in source
    assert "IN ['', $project_id]" not in source
    assert "IN ['', $pid]" not in source
