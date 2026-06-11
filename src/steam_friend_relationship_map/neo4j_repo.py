from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from datetime import UTC, datetime, timedelta

from neo4j import GraphDatabase
from .models import (
    CrawlRun,
    CrawlStatus,
    DbStats,
    ExportResponse,
    FriendCircleAnalysisResponse,
    FriendCircleCandidate,
    FriendEdge,
    GraphEdge,
    GraphNode,
    GraphResponse,
    SteamUserRecord,
    utc_now_iso,
)


class Neo4jRepository:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def test_connection(self) -> str:
        self.driver.verify_connectivity()
        return "Neo4j 连接正常"

    def ensure_schema(self) -> None:
        # 约束保证 MERGE 的唯一键稳定，也能让后续查询更快。
        statements = [
            "CREATE CONSTRAINT steam_user_id IF NOT EXISTS FOR (u:SteamUser) REQUIRE u.steam_id IS UNIQUE",
            "CREATE CONSTRAINT crawl_run_id IF NOT EXISTS FOR (r:CrawlRun) REQUIRE r.id IS UNIQUE",
        ]
        with self.driver.session() as session:
            for statement in statements:
                session.run(statement).consume()

    def start_crawl_run(self, run: CrawlRun) -> None:
        with self.driver.session() as session:
            session.run(
                """
                MERGE (r:CrawlRun {id: $id})
                SET r.root_steam_id = $root_steam_id,
                    r.max_depth = $max_depth,
                    r.max_nodes = $max_nodes,
                    r.status = $status,
                    r.started_at = $started_at,
                    r.finished_at = $finished_at,
                    r.nodes_discovered = $nodes_discovered,
                    r.edges_discovered = $edges_discovered,
                    r.private_count = $private_count,
                    r.error_count = $error_count,
                    r.message = $message,
                    r.current_depth = $current_depth,
                    r.current_steam_id = $current_steam_id,
                    r.queue_size = $queue_size,
                    r.expanded_count = $expanded_count,
                    r.progress_percent = $progress_percent,
                    r.last_event = $last_event,
                    r.filtered_count = $filtered_count,
                    r.friend_count_filtered_count = $friend_count_filtered_count,
                    r.prior_pool_filtered_count = $prior_pool_filtered_count
                """,
                **run.model_dump(mode="json"),
            ).consume()

    def update_crawl_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"r.{key} = ${key}" for key in fields)
        with self.driver.session() as session:
            session.run(f"MATCH (r:CrawlRun {{id: $run_id}}) SET {assignments}", run_id=run_id, **fields).consume()

    def get_crawl_run(self, run_id: str) -> CrawlRun | None:
        with self.driver.session() as session:
            record = session.run("MATCH (r:CrawlRun {id: $run_id}) RETURN r", run_id=run_id).single()
        if record is None:
            return None
        return CrawlRun(**dict(record["r"]))

    def upsert_users(self, users: Iterable[SteamUserRecord]) -> None:
        rows = [user.model_dump(mode="json") for user in users]
        if not rows:
            return
        now = utc_now_iso()
        with self.driver.session() as session:
            # 用户节点用 steam_id 幂等写入，备注/标签/分类由本工具维护，不被 Steam 资料覆盖。
            session.run(
                """
                UNWIND $users AS user
                MERGE (u:SteamUser {steam_id: user.steam_id})
                ON CREATE SET u.first_seen_at = $now
                SET u.last_seen_at = $now,
                    u.persona_name = user.persona_name,
                    u.profile_url = user.profile_url,
                    u.avatar = user.avatar,
                    u.avatar_medium = user.avatar_medium,
                    u.avatar_full = user.avatar_full,
                    u.visibility_state = user.visibility_state,
                    u.profile_state = user.profile_state,
                    u.friend_count = CASE
                        WHEN user.friend_count IS NULL THEN u.friend_count
                        ELSE user.friend_count
                    END,
                    u.friend_count_status = CASE
                        WHEN user.friend_count_status IS NULL OR user.friend_count_status = "unknown" THEN coalesce(u.friend_count_status, "unknown")
                        ELSE user.friend_count_status
                    END,
                    u.prior_pool_link_count = CASE
                        WHEN user.prior_pool_link_count > coalesce(u.prior_pool_link_count, 0) THEN user.prior_pool_link_count
                        ELSE coalesce(u.prior_pool_link_count, 0)
                    END,
                    u.root_closeness_score = CASE
                        WHEN user.root_closeness_score > coalesce(u.root_closeness_score, 0) THEN user.root_closeness_score
                        ELSE coalesce(u.root_closeness_score, 0)
                    END,
                    u.last_scored_crawl_id = CASE
                        WHEN user.last_scored_crawl_id = "" THEN coalesce(u.last_scored_crawl_id, "")
                        ELSE user.last_scored_crawl_id
                    END,
                    u.friend_list_status = CASE
                        WHEN coalesce(u.friend_list_status, "unknown") = "private" THEN "private"
                        ELSE user.friend_list_status
                    END,
                    u.depth_min = CASE
                        WHEN u.depth_min IS NULL OR user.depth_min < u.depth_min THEN user.depth_min
                        ELSE u.depth_min
                    END,
                    u.note = coalesce(u.note, ""),
                    u.tags = coalesce(u.tags, []),
                    u.category = coalesce(u.category, "")
                """,
                users=rows,
                now=now,
            ).consume()

    def mark_friend_list_status(
        self,
        steam_id: str,
        status: str,
        *,
        friend_count: int | None = None,
        friend_count_status: str | None = None,
    ) -> None:
        with self.driver.session() as session:
            session.run(
                """
                MERGE (u:SteamUser {steam_id: $steam_id})
                SET u.friend_list_status = $status,
                    u.friend_count = CASE
                        WHEN $friend_count IS NULL THEN u.friend_count
                        ELSE $friend_count
                    END,
                    u.friend_count_status = CASE
                        WHEN $friend_count_status IS NULL THEN coalesce(u.friend_count_status, "unknown")
                        ELSE $friend_count_status
                    END,
                    u.friend_list_fetched_at = $now,
                    u.last_seen_at = $now
                """,
                steam_id=steam_id,
                status=status,
                friend_count=friend_count,
                friend_count_status=friend_count_status,
                now=utc_now_iso(),
            ).consume()

    def get_cached_friend_list(self, steam_id: str, valid_days: int) -> tuple[str, list[str]] | None:
        if valid_days <= 0:
            return None
        cutoff_time = (datetime.now(UTC) - timedelta(days=valid_days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.driver.session() as session:
            record = session.run(
                """
                MATCH (u:SteamUser {steam_id: $steam_id})
                WHERE u.friend_list_fetched_at >= $cutoff_time
                RETURN u.friend_list_status AS status
                """,
                steam_id=steam_id,
                cutoff_time=cutoff_time,
            ).single()
            if not record:
                return None
            status = record["status"] or "unknown"
            if status != "public":
                return status, []
            
            friends = session.run(
                """
                MATCH (u:SteamUser {steam_id: $steam_id})-[:STEAM_FRIEND]-(f:SteamUser)
                RETURN f.steam_id AS friend_id
                """,
                steam_id=steam_id,
            )
            return status, [row["friend_id"] for row in friends]

    def upsert_relationships(self, edges: Iterable[FriendEdge]) -> None:
        rows = [edge.model_dump(mode="json") for edge in edges]
        if not rows:
            return
        now = utc_now_iso()
        with self.driver.session() as session:
            # Steam 好友关系按无向边处理，避免 A-B 和 B-A 重复出现。
            session.run(
                """
                UNWIND $edges AS edge
                MATCH (a:SteamUser {steam_id: edge.from_id})
                MATCH (b:SteamUser {steam_id: edge.to_id})
                MERGE (a)-[r:STEAM_FRIEND]-(b)
                ON CREATE SET r.first_seen_at = $now
                SET r.last_seen_at = $now,
                    r.crawl_id = edge.crawl_id,
                    r.source_depth = edge.source_depth
                """,
                edges=rows,
                now=now,
            ).consume()

    def patch_user(self, steam_id: str, *, note: str | None = None, tags: list[str] | None = None, category: str | None = None) -> None:
        fields: dict[str, Any] = {}
        if note is not None:
            fields["note"] = note
        if tags is not None:
            fields["tags"] = tags
        if category is not None:
            fields["category"] = category
        if not fields:
            return
        assignments = ", ".join(f"u.{key} = ${key}" for key in fields)
        with self.driver.session() as session:
            session.run(
                f"MATCH (u:SteamUser {{steam_id: $steam_id}}) SET {assignments}, u.last_seen_at = $now",
                steam_id=steam_id,
                now=utc_now_iso(),
                **fields,
            ).consume()

    def get_graph(
        self,
        *,
        root: str | None,
        depth: int,
        limit: int,
        query: str | None = None,
        category: str | None = None,
        friend_count_min: int | None = None,
        friend_count_max: int | None = None,
        prior_pool_min_links: int = 0,
        sort_by: str = "depth",
        sort_dir: str = "asc",
    ) -> GraphResponse:
        depth = max(0, min(depth, 4))
        limit = max(1, min(limit, 2000))
        filters = []
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["query"] = query.lower()
            filters.append("(toLower(coalesce(n.persona_name, '')) CONTAINS $query OR n.steam_id CONTAINS $query)")
        if category:
            params["category"] = category
            filters.append("coalesce(n.category, '') = $category")
        if friend_count_min is not None:
            params["friend_count_min"] = friend_count_min
            filters.append("coalesce(n.friend_count, -1) >= $friend_count_min")
        if friend_count_max is not None:
            params["friend_count_max"] = friend_count_max
            filters.append("coalesce(n.friend_count, -1) <= $friend_count_max")
        if prior_pool_min_links:
            params["prior_pool_min_links"] = prior_pool_min_links
            filters.append("coalesce(n.prior_pool_link_count, 0) >= $prior_pool_min_links")
        where = "WHERE " + " AND ".join(filters) if filters else ""
        sort_map = {
            "depth": "coalesce(n.depth_min, 999)",
            "degree": "degree",
            "friend_count": "coalesce(n.friend_count, -1)",
            "prior_pool_links": "coalesce(n.prior_pool_link_count, 0)",
            "closeness": "coalesce(n.root_closeness_score, 0)",
        }
        order_expr = sort_map.get(sort_by, sort_map["depth"])
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
        with self.driver.session() as session:
            if root:
                params["root"] = root
                # Root 查询只取指定层数内的子图，防止前端一次渲染过大的全库图。
                node_query = f"""
                MATCH p=(r:SteamUser {{steam_id: $root}})-[:STEAM_FRIEND*0..{depth}]-(n:SteamUser)
                WITH DISTINCT n
                {where}
                RETURN n, COUNT {{ (n)-[:STEAM_FRIEND]-() }} AS degree
                ORDER BY {order_expr} {direction}, degree DESC
                LIMIT $limit + 1
                """
            else:
                node_query = f"""
                MATCH (n:SteamUser)
                {where}
                RETURN n, COUNT {{ (n)-[:STEAM_FRIEND]-() }} AS degree
                ORDER BY {order_expr} {direction}, degree DESC
                LIMIT $limit + 1
                """
            records = list(session.run(node_query, **params))
            limited = len(records) > limit
            records = records[:limit]
            nodes = [self._graph_node(record["n"], record["degree"]) for record in records]
            ids = [node.id for node in nodes]
            edge_records = list(
                session.run(
                    """
                    MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                    WHERE a.steam_id IN $ids AND b.steam_id IN $ids AND a.steam_id < b.steam_id
                    RETURN a.steam_id AS source,
                           b.steam_id AS target,
                           COUNT { (a)-[:STEAM_FRIEND]-(:SteamUser)-[:STEAM_FRIEND]-(b) } AS strength
                    LIMIT 5000
                    """,
                    ids=ids,
                )
            )
        edges = [
            GraphEdge(id=f"{record['source']}-{record['target']}", source=record["source"], target=record["target"], strength=record["strength"] or 1)
            for record in edge_records
        ]
        return GraphResponse(nodes=nodes, edges=edges, limited=limited)

    def get_shortest_path(self, from_id: str, to_id: str, max_depth: int) -> GraphResponse:
        max_depth = max(1, min(max_depth, 4))
        with self.driver.session() as session:
            record = session.run(
                f"""
                MATCH p=shortestPath((a:SteamUser {{steam_id: $from_id}})-[:STEAM_FRIEND*..{max_depth}]-(b:SteamUser {{steam_id: $to_id}}))
                RETURN nodes(p) AS nodes, relationships(p) AS rels
                """,
                from_id=from_id,
                to_id=to_id,
            ).single()
            if record is None:
                return GraphResponse(nodes=[], edges=[])
            nodes = [self._graph_node(node, 0) for node in record["nodes"]]
            edges = []
            path_nodes = record["nodes"]
            for index in range(len(path_nodes) - 1):
                source = path_nodes[index]["steam_id"]
                target = path_nodes[index + 1]["steam_id"]
                edges.append(GraphEdge(id=f"{source}-{target}", source=source, target=target, strength=1))
            return GraphResponse(nodes=nodes, edges=edges)

    def get_friend_circle_analysis(self, root: str, max_depth: int = 3, min_mutual: int = 2, limit: int = 50) -> FriendCircleAnalysisResponse:
        max_depth = max(2, min(max_depth, 4))
        min_mutual = max(0, min_mutual)
        limit = max(1, min(limit, 100))
        with self.driver.session() as session:
            records = list(
                session.run(
                    f"""
                    MATCH (root:SteamUser {{steam_id: $root}})
                    MATCH p=(root)-[:STEAM_FRIEND*2..{max_depth}]-(candidate:SteamUser)
                    WITH root, candidate, min(length(p)) AS depth
                    WHERE candidate.steam_id <> $root
                      AND NOT EXISTS {{
                        MATCH (root)-[:STEAM_FRIEND]-(candidate)
                      }}
                    MATCH (candidate)-[:STEAM_FRIEND]-(evidence:SteamUser)
                    WHERE coalesce(evidence.depth_min, 999) < coalesce(candidate.depth_min, 999)
                       OR EXISTS {{
                        MATCH (root)-[:STEAM_FRIEND]-(evidence)
                       }}
                    WITH candidate,
                         depth,
                         collect(DISTINCT evidence)[0..6] AS evidence_nodes,
                         count(DISTINCT evidence) AS mutual_count,
                         COUNT {{ (candidate)-[:STEAM_FRIEND]-() }} AS degree
                    WHERE mutual_count >= $min_mutual
                    RETURN candidate,
                           depth,
                           evidence_nodes,
                           mutual_count,
                           degree,
                           (mutual_count * 10 + degree * 0.2 + coalesce(candidate.friend_count, 0) / 100.0 - depth * 3) AS score
                    ORDER BY score DESC, mutual_count DESC
                    LIMIT $limit
                    """,
                    root=root,
                    min_mutual=min_mutual,
                    limit=limit,
                )
            )
        candidates = []
        for record in records:
            node = self._graph_node(record["candidate"], record["degree"])
            candidates.append(
                FriendCircleCandidate(
                    steam_id=node.id,
                    label=node.label,
                    depth=record["depth"],
                    avatar=node.avatar,
                    profile_url=node.profile_url,
                    degree=node.degree,
                    friend_count=node.friend_count,
                    mutual_count=record["mutual_count"],
                    score=round(float(record["score"] or 0), 2),
                    evidence=[self._graph_node(evidence, 0) for evidence in record["evidence_nodes"]],
                )
            )
        return FriendCircleAnalysisResponse(root=root, candidates=candidates)

    def get_top_degree(self, limit: int = 12) -> list[GraphNode]:
        with self.driver.session() as session:
            records = list(
                session.run(
                    """
                    MATCH (n:SteamUser)
                    RETURN n, COUNT { (n)-[:STEAM_FRIEND]-() } AS degree
                    ORDER BY degree DESC
                    LIMIT $limit
                    """,
                    limit=max(1, min(limit, 50)),
                )
            )
        return [self._graph_node(record["n"], record["degree"]) for record in records]

    def get_db_stats(self) -> DbStats:
        with self.driver.session() as session:
            steam_users = session.run("MATCH (u:SteamUser) RETURN count(u) AS count").single()["count"]
            relationships = session.run("MATCH ()-[r:STEAM_FRIEND]->() RETURN count(r) AS count").single()["count"]
            crawl_runs = session.run("MATCH (c:CrawlRun) RETURN count(c) AS count").single()["count"]
            latest_record = session.run(
                """
                MATCH (latest:CrawlRun)
                RETURN latest
                ORDER BY latest.started_at DESC
                LIMIT 1
                """
            ).single()
        latest = latest_record["latest"] if latest_record is not None else None
        return DbStats(
            steam_users=steam_users or 0,
            steam_friend_relationships=relationships or 0,
            crawl_runs=crawl_runs or 0,
            latest_crawl=CrawlRun(**dict(latest)) if latest is not None else None,
        )

    def export_graph(self) -> ExportResponse:
        with self.driver.session() as session:
            nodes = [dict(record["n"]) for record in session.run("MATCH (n:SteamUser) RETURN n ORDER BY n.depth_min, n.persona_name")]
            edges = [
                {"source": record["source"], "target": record["target"]}
                for record in session.run(
                    """
                    MATCH (a:SteamUser)-[:STEAM_FRIEND]-(b:SteamUser)
                    WHERE a.steam_id < b.steam_id
                    RETURN a.steam_id AS source, b.steam_id AS target
                    ORDER BY source, target
                    """
                )
            ]
        return ExportResponse(nodes=nodes, edges=edges)

    @staticmethod
    def _graph_node(node: Any, degree: int) -> GraphNode:
        data = dict(node)
        return GraphNode(
            id=data.get("steam_id", ""),
            label=data.get("persona_name") or data.get("steam_id", "Unknown"),
            depth=data.get("depth_min"),
            avatar=data.get("avatar_full") or data.get("avatar_medium") or data.get("avatar") or "",
            profile_url=data.get("profile_url") or "",
            note=data.get("note") or "",
            tags=data.get("tags") or [],
            category=data.get("category") or "",
            friend_list_status=data.get("friend_list_status") or "unknown",
            degree=degree,
            friend_count=data.get("friend_count"),
            friend_count_status=data.get("friend_count_status") or "unknown",
            prior_pool_link_count=data.get("prior_pool_link_count") or 0,
            root_closeness_score=data.get("root_closeness_score") or 0,
        )
