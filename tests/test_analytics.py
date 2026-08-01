from __future__ import annotations

import pytest

from steam_friend_relationship_map.analytics import analyze_network
from steam_friend_relationship_map.models import ExportResponse


def _node(steam_id: str, name: str | None = None) -> dict[str, str]:
    return {
        "steam_id": steam_id,
        "persona_name": name or steam_id.upper(),
        "avatar_full": f"https://example.com/{steam_id}.jpg",
        "profile_url": f"https://steamcommunity.com/profiles/{steam_id}",
    }


def test_network_analysis_finds_influential_bridge_nodes_and_communities() -> None:
    data = ExportResponse(
        nodes=[_node(steam_id) for steam_id in "abcdef"],
        edges=[
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
            {"source": "c", "target": "a"},
            {"source": "c", "target": "d"},
            {"source": "d", "target": "e"},
            {"source": "e", "target": "f"},
            {"source": "f", "target": "d"},
        ],
    )

    result = analyze_network(data, limit=6)
    metrics = {metric.id: metric for metric in result.metrics}

    assert result.analyzed_nodes == 6
    assert result.analyzed_edges == 7
    assert result.community_count == 2
    assert result.modularity > 0
    assert metrics["a"].community == metrics["b"].community == metrics["c"].community
    assert metrics["d"].community == metrics["e"].community == metrics["f"].community
    assert metrics["c"].community != metrics["d"].community
    assert metrics["c"].pagerank > metrics["a"].pagerank
    assert metrics["d"].pagerank > metrics["e"].pagerank
    assert {result.leaders[0].id, result.leaders[1].id} == {"c", "d"}


def test_network_analysis_handles_isolated_nodes_deterministically() -> None:
    data = ExportResponse(nodes=[_node("c"), _node("a"), _node("b")], edges=[])

    result = analyze_network(data, limit=2)

    assert result.analyzed_nodes == 3
    assert result.analyzed_edges == 0
    assert result.community_count == 3
    assert result.modularity == 0
    assert [leader.id for leader in result.leaders] == ["a", "b"]
    assert all(metric.pagerank == pytest.approx(1 / 3) for metric in result.metrics)
    assert all(metric.community_size == 1 for metric in result.metrics)


def test_network_analysis_ignores_invalid_edges_self_loops_and_duplicates() -> None:
    data = ExportResponse(
        nodes=[_node("a"), _node("b"), _node("a", "Duplicate"), {"persona_name": "Missing ID"}],
        edges=[
            {"source": "a", "target": "b"},
            {"source": "b", "target": "a"},
            {"source": "a", "target": "a"},
            {"source": "a", "target": "missing"},
            {"source": "", "target": "b"},
        ],
    )

    result = analyze_network(data, limit=1)

    assert result.analyzed_nodes == 2
    assert result.analyzed_edges == 1
    assert len(result.metrics) == 2
    assert len(result.leaders) == 1


def test_network_analysis_returns_empty_response_for_empty_export() -> None:
    result = analyze_network(ExportResponse(nodes=[], edges=[]))

    assert result.analyzed_nodes == 0
    assert result.metrics == []
    assert result.leaders == []
