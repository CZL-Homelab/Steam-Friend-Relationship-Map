from __future__ import annotations

from typing import Any

import networkx as nx

from .models import (
    ExportResponse,
    NetworkAnalysisResponse,
    NetworkLeader,
    NetworkMetric,
)


def _node_id(value: Any) -> str:
    return str(value or "").strip()


def analyze_network(
    data: ExportResponse,
    *,
    limit: int = 12,
    resolution: float = 1.0,
    seed: int = 42,
) -> NetworkAnalysisResponse:
    """Calculate deterministic influence and community metrics for an exported graph."""
    graph = nx.Graph()
    node_records: dict[str, dict[str, Any]] = {}

    for raw_node in data.nodes:
        steam_id = _node_id(raw_node.get("steam_id"))
        if not steam_id or steam_id in node_records:
            continue
        node_records[steam_id] = raw_node
        graph.add_node(steam_id)

    for raw_edge in data.edges:
        source = _node_id(raw_edge.get("source"))
        target = _node_id(raw_edge.get("target"))
        if source == target or source not in node_records or target not in node_records:
            continue
        graph.add_edge(source, target)

    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()
    if not node_count:
        return NetworkAnalysisResponse()

    if edge_count:
        try:
            pagerank = nx.pagerank(graph, alpha=0.85, max_iter=200, tol=1e-8)
        except nx.PowerIterationFailedConvergence:
            pagerank = nx.pagerank(graph, alpha=0.85, max_iter=1000, tol=1e-6)
        raw_communities = nx.community.louvain_communities(
            graph,
            resolution=resolution,
            seed=seed,
        )
    else:
        pagerank = {steam_id: 1 / node_count for steam_id in graph.nodes}
        raw_communities = [{steam_id} for steam_id in graph.nodes]

    communities = sorted(
        (sorted(community) for community in raw_communities),
        key=lambda members: (-len(members), members[0]),
    )
    community_by_id: dict[str, tuple[int, int]] = {}
    for community_id, members in enumerate(communities, start=1):
        for steam_id in members:
            community_by_id[steam_id] = (community_id, len(members))

    degree_by_id = dict(graph.degree())
    metrics = [
        NetworkMetric(
            id=steam_id,
            pagerank=round(float(pagerank[steam_id]), 10),
            community=community_by_id[steam_id][0],
            community_size=community_by_id[steam_id][1],
            degree=degree_by_id[steam_id],
        )
        for steam_id in sorted(graph.nodes)
    ]
    metrics_by_id = {metric.id: metric for metric in metrics}

    ranked_ids = sorted(
        graph.nodes,
        key=lambda steam_id: (
            -pagerank[steam_id],
            -degree_by_id[steam_id],
            str(node_records[steam_id].get("persona_name") or steam_id).casefold(),
            steam_id,
        ),
    )[:limit]
    leaders = []
    for steam_id in ranked_ids:
        raw_node = node_records[steam_id]
        metric = metrics_by_id[steam_id]
        leaders.append(
            NetworkLeader(
                id=steam_id,
                label=str(raw_node.get("persona_name") or steam_id),
                avatar=str(
                    raw_node.get("avatar_full")
                    or raw_node.get("avatar_medium")
                    or raw_node.get("avatar")
                    or ""
                ),
                profile_url=str(raw_node.get("profile_url") or ""),
                pagerank=metric.pagerank,
                degree=metric.degree,
                community=metric.community,
                community_size=metric.community_size,
            )
        )

    modularity = (
        nx.community.modularity(graph, [set(members) for members in communities])
        if edge_count
        else 0
    )
    return NetworkAnalysisResponse(
        metrics=metrics,
        leaders=leaders,
        analyzed_nodes=node_count,
        analyzed_edges=edge_count,
        community_count=len(communities),
        modularity=round(float(modularity), 8),
    )
