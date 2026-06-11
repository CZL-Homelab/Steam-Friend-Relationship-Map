from __future__ import annotations

from pathlib import Path


def test_neo4j_queries_do_not_use_removed_size_pattern() -> None:
    source = Path("src/steam_friend_relationship_map/neo4j_repo.py").read_text(encoding="utf-8")

    assert "size((n)-[:STEAM_FRIEND]-())" not in source
    assert "COUNT { (n)-[:STEAM_FRIEND]-() }" in source
