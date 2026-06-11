from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from neo4j import GraphDatabase
from .models import CrawlRun, CrawlStatus, DbStats, ExportResponse, FriendEdge, GraphEdge, GraphNode, GraphResponse, SteamUserRecord, utc_now_iso


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
                    r.message = $message
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

    def mark_friend_list_status(self, steam_id: str, status: str) -> None:
        with self.driver.session() as session:
            session.run(
                """
                MERGE (u:SteamUser {steam_id: $steam_id})
                SET u.friend_list_status = $status,
                    u.last_seen_at = $now
                """,
                steam_id=steam_id,
                status=status,
                now=utc_now_iso(),
            ).consume()

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
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self.driver.session() as session:
            if root:
                params["root"] = root
                # Root 查询只取指定层数内的子图，防止前端一次渲染过大的全库图。
                node_query = f"""
                MATCH p=(r:SteamUser {{steam_id: $root}})-[:STEAM_FRIEND*0..{depth}]-(n:SteamUser)
                WITH DISTINCT n
                {where}
                RETURN n, COUNT {{ (n)-[:STEAM_FRIEND]-() }} AS degree
                ORDER BY coalesce(n.depth_min, 999), degree DESC
                LIMIT $limit + 1
                """
            else:
                node_query = f"""
                MATCH (n:SteamUser)
                {where}
                RETURN n, COUNT {{ (n)-[:STEAM_FRIEND]-() }} AS degree
                ORDER BY degree DESC
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
                    RETURN a.steam_id AS source, b.steam_id AS target
                    LIMIT 5000
                    """,
                    ids=ids,
                )
            )
        edges = [
            GraphEdge(id=f"{record['source']}-{record['target']}", source=record["source"], target=record["target"])
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
                edges.append(GraphEdge(id=f"{source}-{target}", source=source, target=target))
            return GraphResponse(nodes=nodes, edges=edges)

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
        )
