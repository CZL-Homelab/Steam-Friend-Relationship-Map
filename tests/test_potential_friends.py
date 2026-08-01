from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from steam_friend_relationship_map.kuzu_repo import KuzuRepositoryImpl
from steam_friend_relationship_map.models import FriendEdge, ProjectCreate, SteamUserRecord

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

def test_kuzu_get_potential_friends(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    # Create network:
    # 1 (Root) is friends with 2, 3
    # 4 is friends with 2, 3 (Potential friend of 1: mutuals = {2, 3})
    # 5 is friends with 2 (Potential friend of 1: mutuals = {2})
    repo.upsert_users([
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="2", persona_name="Bob", depth_min=1),
        SteamUserRecord(steam_id="3", persona_name="Charlie", depth_min=1),
        SteamUserRecord(steam_id="4", persona_name="David", depth_min=2),
        SteamUserRecord(steam_id="5", persona_name="Eve", depth_min=2),
    ], "default")

    repo.upsert_relationships([
        FriendEdge(from_id="1", to_id="2", crawl_id="run-1", source_depth=0),
        FriendEdge(from_id="1", to_id="3", crawl_id="run-1", source_depth=0),
        FriendEdge(from_id="2", to_id="4", crawl_id="run-1", source_depth=1),
        FriendEdge(from_id="3", to_id="4", crawl_id="run-1", source_depth=1),
        FriendEdge(from_id="2", to_id="5", crawl_id="run-1", source_depth=1),
    ], "default")

    # Mark friend list statuses to build degrees
    repo.mark_friend_list_status("1", "public", friend_count=2, friend_count_status="public", friend_ids=["2", "3"], project_id="default")
    repo.mark_friend_list_status("2", "public", friend_count=3, friend_count_status="public", friend_ids=["1", "4", "5"], project_id="default")
    repo.mark_friend_list_status("3", "public", friend_count=2, friend_count_status="public", friend_ids=["1", "4"], project_id="default")

    res = repo.get_potential_friends(root="1", max_depth=3, min_mutual=1, limit=5, project_id="default")
    assert len(res.candidates) == 2

    # David (ID 4) has mutuals: {2, 3}. Alice degree = 2. David degree = 2.
    # Union = 2 + 2 - 2 = 2. Jaccard = 2/2 = 1.0. Score = 100.
    david = [c for c in res.candidates if c.steam_id == "4"][0]
    assert david.mutual_count == 2
    assert david.jaccard_coefficient == 1.0
    assert david.score == 100.0

    # Eve (ID 5) has mutuals: {2}. Alice degree = 2. Eve degree = 1.
    # Union = 2 + 1 - 1 = 2. Jaccard = 1/2 = 0.5. Score = 50.
    eve = [c for c in res.candidates if c.steam_id == "5"][0]
    assert eve.mutual_count == 1
    assert eve.jaccard_coefficient == 0.5
    assert eve.score == 50.0


def test_kuzu_multi_root_graph(temp_kuzu_repo: KuzuRepositoryImpl) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()

    # Create network:
    # 1 (Root A) is friends with 2
    # 3 (Root B) is friends with 2
    # Node 2 is the intersection node!
    repo.upsert_users([
        SteamUserRecord(steam_id="1", persona_name="Alice", depth_min=0),
        SteamUserRecord(steam_id="2", persona_name="Bob", depth_min=1),
        SteamUserRecord(steam_id="3", persona_name="Charlie", depth_min=0),
    ], "default")

    repo.upsert_relationships([
        FriendEdge(from_id="1", to_id="2", crawl_id="run-1", source_depth=0),
        FriendEdge(from_id="3", to_id="2", crawl_id="run-1", source_depth=0),
    ], "default")

    # Mark statuses
    repo.mark_friend_list_status("1", "public", friend_count=1, friend_count_status="public", friend_ids=["2"], project_id="default")
    repo.mark_friend_list_status("3", "public", friend_count=1, friend_count_status="public", friend_ids=["2"], project_id="default")

    # Query with multiple roots "1,3"
    graph = repo.get_graph(root="1,3", depth=1, limit=10, project_id="default")
    assert len(graph.nodes) == 3

    bob = [n for n in graph.nodes if n.id == "2"][0]
    assert bob.is_intersection is True

    alice = [n for n in graph.nodes if n.id == "1"][0]
    assert alice.is_intersection is False
    charlie = [n for n in graph.nodes if n.id == "3"][0]
    assert alice.root_friend_circle_score == 1_000_000
    assert charlie.root_friend_circle_score == 1_000_000
    assert bob.root_route_count == 2
    assert bob.root_route_total_hops == 2


def test_kuzu_potential_friends_are_project_isolated(
    temp_kuzu_repo: KuzuRepositoryImpl,
) -> None:
    repo = temp_kuzu_repo
    repo.ensure_schema()
    repo.create_project(ProjectCreate(name="Other"), project_id="other")
    users = [
        SteamUserRecord(steam_id="root", persona_name="Root", depth_min=0),
        SteamUserRecord(steam_id="mutual", persona_name="Mutual", depth_min=1),
        SteamUserRecord(steam_id="candidate", persona_name="Candidate", depth_min=2),
    ]
    repo.upsert_users(users, "default")
    repo.upsert_users(users, "other")
    repo.upsert_relationships(
        [FriendEdge(from_id="root", to_id="mutual", crawl_id="run-a", source_depth=0)],
        "default",
    )
    repo.upsert_relationships(
        [
            FriendEdge(from_id="root", to_id="mutual", crawl_id="run-b", source_depth=0),
            FriendEdge(
                from_id="mutual",
                to_id="candidate",
                crawl_id="run-b",
                source_depth=1,
            ),
        ],
        "other",
    )

    result = repo.get_potential_friends(
        root="root", min_mutual=1, project_id="default"
    )

    assert result.candidates == []
