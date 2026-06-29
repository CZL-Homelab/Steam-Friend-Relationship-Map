from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any

import kuzu
from .graph_repo import IGraphRepository
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
    ProjectCreate,
    ProjectInfo,
    ProjectListResponse,
    SteamUserRecord,
    utc_now_iso,
)


def _parse_node(row_val: Any) -> dict[str, Any]:
    """将 Kùzu 查询返回的节点转换为标准的 Python 字典。"""
    if isinstance(row_val, dict):
        return row_val
    if hasattr(row_val, "properties") and isinstance(row_val.properties, dict):
        return row_val.properties
    return {}


class KuzuRepositoryImpl(IGraphRepository):
    """Kùzu 进程内嵌入式图数据库实现。"""

    def __init__(self, db_path: str, buffer_pool_size_gb: int = 1) -> None:
        self.db_path = db_path
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        buffer_pool_size_bytes = int(buffer_pool_size_gb * 1024 * 1024 * 1024)
        self.db = kuzu.Database(db_path, buffer_pool_size=buffer_pool_size_bytes)

    def _get_conn(self) -> kuzu.Connection:
        """获取一个独立的连接。因为 Kùzu 连接是非线程安全的，在此动态实例化。"""
        return kuzu.Connection(self.db)

    def close(self) -> None:
        """Kùzu 引擎生命周期由系统垃圾回收管理，此处仅作空实现。"""
        pass

    def test_connection(self) -> str:
        conn = self._get_conn()
        res = conn.execute("RETURN 1")
        if res.has_next():
            return "Kùzu 连接正常"
        raise RuntimeError("Kùzu 连接异常")

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        res = conn.execute("CALL show_tables() RETURN name")
        existing_tables = set()
        while res.has_next():
            existing_tables.add(res.get_next()[0])

        if "SteamUser" not in existing_tables:
            conn.execute("""
                CREATE NODE TABLE SteamUser(
                    steam_id STRING,
                    persona_name STRING,
                    profile_url STRING,
                    avatar STRING,
                    avatar_medium STRING,
                    avatar_full STRING,
                    visibility_state INT64,
                    profile_state INT64,
                    depth_min INT64,
                    friend_list_status STRING,
                    friend_count INT64,
                    friend_count_status STRING,
                    prior_pool_link_count INT64,
                    root_closeness_score DOUBLE,
                    last_scored_crawl_id STRING,
                    project_id STRING,
                    note STRING,
                    tags STRING[],
                    category STRING,
                    last_seen_at STRING,
                    first_seen_at STRING,
                    friend_ids STRING[],
                    friend_list_fetched_at STRING,
                    PRIMARY KEY(steam_id)
                )
            """)

        if "CrawlRun" not in existing_tables:
            conn.execute("""
                CREATE NODE TABLE CrawlRun(
                    id STRING,
                    root_steam_id STRING,
                    max_depth INT64,
                    max_nodes INT64,
                    status STRING,
                    started_at STRING,
                    finished_at STRING,
                    nodes_discovered INT64,
                    edges_discovered INT64,
                    private_count INT64,
                    error_count INT64,
                    message STRING,
                    current_depth INT64,
                    current_steam_id STRING,
                    queue_size INT64,
                    expanded_count INT64,
                    progress_percent INT64,
                    last_event STRING,
                    filtered_count INT64,
                    friend_count_filtered_count INT64,
                    prior_pool_filtered_count INT64,
                    project_id STRING,
                    PRIMARY KEY(id)
                )
            """)

        if "Project" not in existing_tables:
            conn.execute("""
                CREATE NODE TABLE Project(
                    id STRING,
                    name STRING,
                    created_at STRING,
                    PRIMARY KEY(id)
                )
            """)

        if "STEAM_FRIEND" not in existing_tables:
            conn.execute("""
                CREATE REL TABLE STEAM_FRIEND(
                    FROM SteamUser TO SteamUser,
                    crawl_id STRING,
                    source_depth INT64,
                    project_id STRING
                )
            """)

    def ensure_default_project(self) -> str:
        return self.create_project(ProjectCreate(name="默认项目"), project_id="default")

    def create_project(self, payload: ProjectCreate, project_id: str | None = None) -> str:
        import uuid
        pid = project_id or str(uuid.uuid4())
        now = utc_now_iso()
        conn = self._get_conn()
        conn.execute(
            """
            MERGE (p:Project {id: $pid})
            ON CREATE SET p.name = $name, p.created_at = $now
            ON MATCH SET p.name = $name
            """,
            {"pid": pid, "name": payload.name, "now": now}
        )
        return pid

    def delete_project(self, project_id: str) -> bool:
        if project_id == "default":
            return False
        conn = self._get_conn()
        # 显式查验项目是否存在
        if not self.project_exists(project_id):
            return False

        try:
            conn.execute("BEGIN TRANSACTION")
            # Kùzu 必须优先删除关系，再删除相关的 Node，保障引用一致性
            conn.execute("MATCH ()-[r:STEAM_FRIEND]->() WHERE r.project_id = $pid DELETE r", {"pid": project_id})
            conn.execute("MATCH (u:SteamUser) WHERE u.project_id = $pid DELETE u", {"pid": project_id})
            conn.execute("MATCH (r:CrawlRun) WHERE r.project_id = $pid DELETE r", {"pid": project_id})
            conn.execute("MATCH (p:Project) WHERE p.id = $pid DELETE p", {"pid": project_id})
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def project_exists(self, project_id: str) -> bool:
        conn = self._get_conn()
        res = conn.execute("MATCH (p:Project) WHERE p.id = $pid RETURN p.id", {"pid": project_id})
        return res.has_next()

    def list_projects(self) -> ProjectListResponse:
        conn = self._get_conn()
        res = conn.execute(
            """
            MATCH (p:Project)
            OPTIONAL MATCH (u:SteamUser) WHERE u.project_id = p.id
            OPTIONAL MATCH (c:CrawlRun) WHERE c.project_id = p.id
            WITH p, count(DISTINCT u) AS user_count, count(DISTINCT c) AS crawl_count
            OPTIONAL MATCH ()-[r:STEAM_FRIEND]->() WHERE r.project_id = p.id
            RETURN p.id, p.name, p.created_at, user_count, count(DISTINCT r), crawl_count
            ORDER BY p.created_at DESC
            """
        )
        projects = []
        while res.has_next():
            row = res.get_next()
            projects.append(ProjectInfo(
                id=row[0],
                name=row[1],
                created_at=row[2],
                steam_users=row[3] or 0,
                relationships=row[4] or 0,
                crawl_runs=row[5] or 0
            ))
        if not projects:
            self.ensure_schema()
            self.ensure_default_project()
            return ProjectListResponse(
                projects=[ProjectInfo(id="default", name="默认项目", created_at=utc_now_iso())],
                active_project_id=""
            )
        return ProjectListResponse(projects=projects, active_project_id="")

    def get_crawl_run(self, run_id: str) -> CrawlRun | None:
        conn = self._get_conn()
        res = conn.execute(
            """
            MATCH (r:CrawlRun)
            WHERE r.id = $run_id
            RETURN r
            """,
            {"run_id": run_id}
        )
        if not res.has_next():
            return None
        data = _parse_node(res.get_next()[0])
        if not data:
            return None
        return CrawlRun(
            id=data.get("id", ""),
            root_steam_id=data.get("root_steam_id", ""),
            max_depth=data.get("max_depth", 2),
            max_nodes=data.get("max_nodes", 2000),
            status=CrawlStatus(data.get("status", "pending")),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            nodes_discovered=data.get("nodes_discovered") or 0,
            edges_discovered=data.get("edges_discovered") or 0,
            private_count=data.get("private_count") or 0,
            error_count=data.get("error_count") or 0,
            message=data.get("message") or "",
            current_depth=data.get("current_depth"),
            current_steam_id=data.get("current_steam_id") or "",
            queue_size=data.get("queue_size") or 0,
            expanded_count=data.get("expanded_count") or 0,
            progress_percent=data.get("progress_percent") or 0,
            last_event=data.get("last_event") or "",
            filtered_count=data.get("filtered_count") or 0,
            friend_count_filtered_count=data.get("friend_count_filtered_count") or 0,
            prior_pool_filtered_count=data.get("prior_pool_filtered_count") or 0,
        )

    def start_crawl_run(self, run: CrawlRun, project_id: str) -> None:
        now = utc_now_iso()
        conn = self._get_conn()
        conn.execute(
            """
            MERGE (r:CrawlRun {id: $run_id})
            ON CREATE SET
                r.root_steam_id = $root_steam_id,
                r.max_depth = $max_depth,
                r.max_nodes = $max_nodes,
                r.status = $status,
                r.started_at = $now,
                r.project_id = $project_id,
                r.nodes_discovered = 0,
                r.edges_discovered = 0,
                r.private_count = 0,
                r.error_count = 0,
                r.message = "",
                r.queue_size = 0,
                r.expanded_count = 0,
                r.progress_percent = 0,
                r.last_event = "created",
                r.filtered_count = 0,
                r.friend_count_filtered_count = 0,
                r.prior_pool_filtered_count = 0
            ON MATCH SET
                r.status = $status,
                r.started_at = $now,
                r.project_id = $project_id
            """,
            {
                "run_id": run.id,
                "root_steam_id": run.root_steam_id,
                "max_depth": run.max_depth,
                "max_nodes": run.max_nodes,
                "status": run.status.value,
                "now": now,
                "project_id": project_id,
            }
        )

    def update_crawl_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"r.{key} = ${key}" for key in fields)
        conn = self._get_conn()
        conn.execute(
            f"MATCH (r:CrawlRun {{id: $run_id}}) SET {assignments}",
            {"run_id": run_id, **fields}
        )

    def upsert_users(self, users: Iterable[SteamUserRecord], project_id: str) -> None:
        rows = [user.model_dump(mode="json") for user in users]
        if not rows:
            return
        now = utc_now_iso()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN TRANSACTION")
            for row in rows:
                conn.execute(
                    """
                    MERGE (u:SteamUser {steam_id: $steam_id})
                    ON CREATE SET u.first_seen_at = $now,
                                  u.note = '',
                                  u.tags = CAST([] AS STRING[]),
                                  u.category = '',
                                  u.friend_ids = CAST([] AS STRING[]),
                                  u.friend_list_fetched_at = ''
                    SET u.last_seen_at = $now,
                        u.project_id = CASE
                            WHEN u.project_id IS NULL OR u.project_id = '' THEN $project_id
                            ELSE u.project_id
                        END,
                        u.persona_name = $persona_name,
                        u.profile_url = $profile_url,
                        u.avatar = $avatar,
                        u.avatar_medium = $avatar_medium,
                        u.avatar_full = $avatar_full,
                        u.visibility_state = $visibility_state,
                        u.profile_state = $profile_state,
                        u.friend_count = CASE
                            WHEN $friend_count IS NULL THEN u.friend_count
                            ELSE $friend_count
                        END,
                        u.friend_count_status = CASE
                            WHEN $friend_count_status IS NULL OR $friend_count_status = 'unknown' THEN coalesce(u.friend_count_status, 'unknown')
                            ELSE $friend_count_status
                        END,
                        u.prior_pool_link_count = CASE
                            WHEN $prior_pool_link_count > coalesce(u.prior_pool_link_count, 0) THEN $prior_pool_link_count
                            ELSE coalesce(u.prior_pool_link_count, 0)
                        END,
                        u.root_closeness_score = CASE
                            WHEN $root_closeness_score > coalesce(u.root_closeness_score, 0.0) THEN $root_closeness_score
                            ELSE coalesce(u.root_closeness_score, 0.0)
                        END,
                        u.last_scored_crawl_id = CASE
                            WHEN $last_scored_crawl_id = '' THEN coalesce(u.last_scored_crawl_id, '')
                            ELSE $last_scored_crawl_id
                        END,
                        u.friend_list_status = CASE
                            WHEN $friend_list_status = 'unknown' THEN coalesce(u.friend_list_status, 'unknown')
                            WHEN coalesce(u.friend_list_status, 'unknown') = 'private' THEN 'private'
                            ELSE $friend_list_status
                        END,
                        u.depth_min = CASE
                            WHEN u.depth_min IS NULL OR $depth_min < u.depth_min THEN $depth_min
                            ELSE u.depth_min
                        END
                    """,
                    {
                        "steam_id": row["steam_id"],
                        "now": now,
                        "project_id": project_id,
                        "persona_name": row["persona_name"],
                        "profile_url": row["profile_url"],
                        "avatar": row["avatar"],
                        "avatar_medium": row["avatar_medium"],
                        "avatar_full": row["avatar_full"],
                        "visibility_state": row["visibility_state"],
                        "profile_state": row["profile_state"],
                        "friend_count": row["friend_count"],
                        "friend_count_status": row["friend_count_status"],
                        "prior_pool_link_count": row["prior_pool_link_count"],
                        "root_closeness_score": row["root_closeness_score"],
                        "last_scored_crawl_id": row["last_scored_crawl_id"],
                        "friend_list_status": row["friend_list_status"],
                        "depth_min": row["depth_min"],
                    }
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def mark_friend_list_status(
        self,
        steam_id: str,
        status: str,
        friend_count: int | None,
        friend_count_status: str,
        friend_ids: list[str],
        project_id: str,
    ) -> None:
        now = utc_now_iso()
        conn = self._get_conn()
        conn.execute(
            """
            MERGE (u:SteamUser {steam_id: $steam_id})
            ON CREATE SET u.first_seen_at = $now,
                          u.note = '',
                          u.tags = CAST([] AS STRING[]),
                          u.category = ''
            SET u.friend_list_status = $status,
                u.project_id = CASE
                    WHEN u.project_id IS NULL OR u.project_id = '' THEN $project_id
                    ELSE u.project_id
                END,
                u.friend_count = CASE
                    WHEN $friend_count IS NULL THEN u.friend_count
                    ELSE $friend_count
                END,
                u.friend_count_status = CASE
                    WHEN $friend_count_status IS NULL THEN coalesce(u.friend_count_status, 'unknown')
                    ELSE $friend_count_status
                END,
                u.friend_ids = CASE
                    WHEN CAST($friend_ids AS STRING[]) IS NULL THEN u.friend_ids
                    ELSE CAST($friend_ids AS STRING[])
                END,
                u.friend_list_fetched_at = $now,
                u.last_seen_at = $now
            """,
            {
                "steam_id": steam_id,
                "status": status,
                "friend_count": friend_count,
                "friend_count_status": friend_count_status,
                "friend_ids": friend_ids,
                "project_id": project_id,
                "now": now,
            }
        )

    def get_cached_friend_list(
        self, steam_id: str, valid_days: int, project_id: str
    ) -> tuple[str, list[str]] | None:
        if valid_days <= 0:
            return None
        cutoff_time = (
            (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=valid_days))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        conn = self._get_conn()
        res = conn.execute(
            """
            MATCH (u:SteamUser)
            WHERE u.steam_id = $steam_id AND u.friend_list_fetched_at >= $cutoff_time
            RETURN u.friend_list_status, u.friend_ids
            """,
            {"steam_id": steam_id, "cutoff_time": cutoff_time}
        )
        if not res.has_next():
            return None
        row = res.get_next()
        return row[0], row[1]

    def upsert_relationships(self, edges: Iterable[FriendEdge], project_id: str) -> None:
        rows = [edge.model_dump(mode="json") for edge in edges]
        if not rows:
            return
        conn = self._get_conn()
        try:
            conn.execute("BEGIN TRANSACTION")
            for row in rows:
                from_id = row["from_id"]
                to_id = row["to_id"]
                if from_id == to_id:
                    continue
                if from_id > to_id:
                    from_id, to_id = to_id, from_id
                conn.execute(
                    """
                    MATCH (a:SteamUser {steam_id: $from_id})
                    MATCH (b:SteamUser {steam_id: $to_id})
                    MERGE (a)-[r:STEAM_FRIEND]->(b)
                    ON CREATE SET r.crawl_id = $crawl_id,
                                  r.source_depth = $source_depth,
                                  r.project_id = $project_id
                    """,
                    {
                        "from_id": from_id,
                        "to_id": to_id,
                        "crawl_id": row["crawl_id"],
                        "source_depth": row["source_depth"],
                        "project_id": project_id,
                    }
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def patch_user(
        self,
        steam_id: str,
        *,
        note: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
    ) -> None:
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
        conn = self._get_conn()
        conn.execute(
            f"MATCH (u:SteamUser {{steam_id: $steam_id}}) SET {assignments}, u.last_seen_at = $now",
            {"steam_id": steam_id, "now": utc_now_iso(), **fields}
        )

    def bulk_patch_users(self, patches: Iterable[dict[str, Any]]) -> None:
        conn = self._get_conn()
        try:
            conn.execute("BEGIN TRANSACTION")
            now = utc_now_iso()
            for patch in patches:
                steam_id = patch.get("steam_id")
                note = patch.get("note")
                tags = patch.get("tags")
                category = patch.get("category")
                
                fields: dict[str, Any] = {}
                if note is not None:
                    fields["note"] = note
                if tags is not None:
                    fields["tags"] = tags
                if category is not None:
                    fields["category"] = category
                if not fields:
                    continue
                
                assignments = ", ".join(f"u.{key} = ${key}" for key in fields)
                conn.execute(
                    f"MATCH (u:SteamUser {{steam_id: $steam_id}}) SET {assignments}, u.last_seen_at = $now",
                    {"steam_id": steam_id, "now": now, **fields}
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def count_inner_layer_links(
        self, candidate_ids: list[str], inner_pool_ids: list[str], project_id: str
    ) -> dict[str, int]:
        if not candidate_ids or not inner_pool_ids:
            return {}
        conn = self._get_conn()
        res = conn.execute(
            """
            MATCH (c:SteamUser)-[r:STEAM_FRIEND]-(inner:SteamUser)
            WHERE c.steam_id IN $candidates AND inner.steam_id IN $inner_pool
              AND coalesce(r.project_id, '') IN ['', $project_id]
            RETURN c.steam_id, count(DISTINCT inner)
            """,
            {"candidates": candidate_ids, "inner_pool": inner_pool_ids, "project_id": project_id}
        )
        out = {cid: 0 for cid in candidate_ids}
        while res.has_next():
            row = res.get_next()
            out[row[0]] = row[1]
        return {k: v for k, v in out.items() if v > 0}

    def _graph_node(self, data: dict[str, Any], degree: int) -> GraphNode:
        return GraphNode(
            id=data.get("steam_id", ""),
            label=data.get("persona_name") or data.get("steam_id", "Unknown"),
            depth=data.get("depth_min"),
            avatar=data.get("avatar_full") or data.get("avatar_medium") or data.get("avatar") or "",
            profile_url=data.get("profile_url") or "",
            note=data.get("note") or "",
            tags=data.get("tags") or [],
            category=data.get("category") or "",
            degree=degree,
            friend_count=data.get("friend_count"),
        )

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
        project_id: str = "default",
    ) -> GraphResponse:
        # Kùzu depth clamp
        depth = max(0, min(depth, 4))
        limit = max(1, min(limit, 2000))
        filters = []
        params = {"project_id": project_id}

        if not root:
            filters.append("(coalesce(n.project_id, '') IN ['', $project_id] OR EXISTS { MATCH (n)-[r:STEAM_FRIEND]-(:SteamUser) WHERE r.project_id = $project_id })")
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

        conn = self._get_conn()
        if root:
            params["root"] = root
            node_query = f"""
            WITH $project_id AS pid, $root AS root_id
            MATCH p=(r:SteamUser {{steam_id: root_id}})-[:STEAM_FRIEND*0..{depth}]-(n:SteamUser)
            WHERE all(rel IN relationships(p) WHERE coalesce(rel.project_id, '') IN ['', pid])
            WITH DISTINCT n, pid
            {where}
            OPTIONAL MATCH (n)-[rel:STEAM_FRIEND]-() WHERE coalesce(rel.project_id, '') IN ['', pid]
            WITH n, count(DISTINCT rel) AS degree
            RETURN n, degree
            ORDER BY {order_expr} {direction}, degree DESC
            LIMIT $limit
            """
            params["limit"] = limit + 1
        else:
            node_query = f"""
            MATCH (n:SteamUser)
            {where}
            OPTIONAL MATCH (n)-[rel:STEAM_FRIEND]-() WHERE coalesce(rel.project_id, '') IN ['', $project_id]
            WITH n, count(DISTINCT rel) AS degree
            RETURN n, degree
            ORDER BY {order_expr} {direction}, degree DESC
            LIMIT $limit
            """
            params["limit"] = limit + 1

        res_nodes = conn.execute(node_query, params)
        records = []
        while res_nodes.has_next():
            records.append(res_nodes.get_next())

        limited = len(records) > limit
        records = records[:limit]
        nodes = [self._graph_node(_parse_node(rec[0]), rec[1]) for rec in records]
        ids = [node.id for node in nodes]

        if not ids:
            return GraphResponse(nodes=[], edges=[])

        res_edges = conn.execute(
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
            WHERE a.steam_id IN $ids AND b.steam_id IN $ids AND a.steam_id < b.steam_id
              AND coalesce(r.project_id, '') IN ['', $project_id]
            RETURN a.steam_id, b.steam_id
            LIMIT 5000
            """,
            {"ids": ids, "project_id": project_id}
        )

        edges = []
        while res_edges.has_next():
            row = res_edges.get_next()
            # Calculate strength locally by counting common neighbors
            res_strength = conn.execute(
                """
                MATCH (a:SteamUser {steam_id: $source})-[:STEAM_FRIEND]-(c:SteamUser)-[:STEAM_FRIEND]-(b:SteamUser {steam_id: $target})
                RETURN count(c)
                """,
                {"source": row[0], "target": row[1]}
            )
            strength = res_strength.get_next()[0] if res_strength.has_next() else 1
            edges.append(GraphEdge(
                id=f"{row[0]}-{row[1]}",
                source=row[0],
                target=row[1],
                strength=max(1, strength)
            ))
        return GraphResponse(nodes=nodes, edges=edges, limited=limited)

    def get_shortest_path(
        self, from_id: str, to_id: str, max_depth: int, project_id: str = "default"
    ) -> GraphResponse:
        max_depth = max(0, min(max_depth, 4))
        conn = self._get_conn()
        res = conn.execute(
            f"""
            WITH $project_id AS pid, $from_id AS fid, $to_id AS tid
            MATCH p=(a:SteamUser {{steam_id: fid}})-[:STEAM_FRIEND*..{max_depth}]-(b:SteamUser {{steam_id: tid}})
            WHERE all(r IN relationships(p) WHERE coalesce(r.project_id, '') IN ['', pid])
            RETURN nodes(p)
            ORDER BY length(p) ASC
            LIMIT 1
            """,
            {"from_id": from_id, "to_id": to_id, "project_id": project_id}
        )
        if not res.has_next():
            return GraphResponse(nodes=[], edges=[])

        path_nodes = res.get_next()[0]
        nodes = []
        for raw_node in path_nodes:
            node_dict = _parse_node(raw_node)
            nodes.append(self._graph_node(node_dict, 0))

        edges = []
        for index in range(len(nodes) - 1):
            source = nodes[index].id
            target = nodes[index + 1].id
            edges.append(GraphEdge(id=f"{source}-{target}", source=source, target=target, strength=1))
        return GraphResponse(nodes=nodes, edges=edges)

    def get_friend_circle_analysis(
        self,
        root: str,
        max_depth: int = 3,
        min_mutual: int = 2,
        limit: int = 50,
        project_id: str = "default",
    ) -> FriendCircleAnalysisResponse:
        max_depth = max(0, min(max_depth, 4))
        min_mutual = max(0, min_mutual)
        limit = max(1, min(limit, 100))
        conn = self._get_conn()

        res = conn.execute(
            f"""
            WITH $project_id AS pid, $root AS root_id
            MATCH (root:SteamUser {{steam_id: root_id}})
            MATCH p=(root)-[:STEAM_FRIEND*2..{max_depth}]-(candidate:SteamUser)
            WHERE all(r IN relationships(p) WHERE coalesce(r.project_id, '') IN ['', pid])
            WITH root, candidate, min(length(p)) AS depth, pid
            WHERE candidate.steam_id <> root.steam_id
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
                 collect(DISTINCT evidence) AS all_evidence,
                 count(DISTINCT evidence) AS mutual_count
            WHERE mutual_count >= $min_mutual
            OPTIONAL MATCH (candidate)-[rel:STEAM_FRIEND]-()
            WITH candidate, depth, all_evidence AS evidence_nodes, mutual_count, count(DISTINCT rel) AS degree
            RETURN candidate,
                   depth,
                   evidence_nodes,
                   mutual_count,
                   degree,
                   (mutual_count * 10 + degree * 0.2 + coalesce(candidate.friend_count, 0) / 100.0 - depth * 3) AS score
            ORDER BY score DESC, mutual_count DESC
            LIMIT $limit
            """,
            {
                "root": root,
                "project_id": project_id,
                "min_mutual": min_mutual,
                "limit": limit
            }
        )

        candidates = []
        while res.has_next():
            row = res.get_next()
            node_dict = _parse_node(row[0])
            node = self._graph_node(node_dict, row[4])
            evidence_list = []
            for ev in row[2][:6]:
                evidence_list.append(self._graph_node(_parse_node(ev), 0))
            candidates.append(
                FriendCircleCandidate(
                    steam_id=node.id,
                    label=node.label,
                    depth=row[1],
                    avatar=node.avatar,
                    profile_url=node.profile_url,
                    degree=node.degree,
                    friend_count=node.friend_count,
                    mutual_count=row[3],
                    score=round(float(row[5] or 0.0), 2),
                    evidence=evidence_list,
                )
            )
        return FriendCircleAnalysisResponse(root=root, candidates=candidates)

    def get_top_degree(self, limit: int = 12, project_id: str = "default") -> list[GraphNode]:
        conn = self._get_conn()
        res = conn.execute(
            """
            MATCH (n:SteamUser)
            WHERE coalesce(n.project_id, '') IN ['', $project_id]
               OR EXISTS { MATCH (n)-[rel:STEAM_FRIEND]-(:SteamUser) WHERE rel.project_id = $project_id }
            OPTIONAL MATCH (n)-[r:STEAM_FRIEND]-()
            WITH n, count(DISTINCT r) AS degree
            RETURN n, degree
            ORDER BY degree DESC
            LIMIT $limit
            """,
            {"project_id": project_id, "limit": limit}
        )
        nodes = []
        while res.has_next():
            row = res.get_next()
            node_dict = _parse_node(row[0])
            nodes.append(self._graph_node(node_dict, row[1]))
        return nodes

    def get_db_stats(self, project_id: str = "default") -> DbStats:
        conn = self._get_conn()
        res_users = conn.execute(
            """
            MATCH (u:SteamUser)
            WHERE coalesce(u.project_id, '') IN ['', $project_id]
               OR EXISTS { MATCH (u)-[r:STEAM_FRIEND]-(:SteamUser) WHERE r.project_id = $project_id }
            RETURN count(u)
            """,
            {"project_id": project_id}
        )
        steam_users = res_users.get_next()[0] if res_users.has_next() else 0

        res_rels = conn.execute(
            "MATCH ()-[r:STEAM_FRIEND]->() WHERE coalesce(r.project_id, '') IN ['', $project_id] RETURN count(r)",
            {"project_id": project_id}
        )
        relationships = res_rels.get_next()[0] if res_rels.has_next() else 0

        res_crawls = conn.execute(
            "MATCH (c:CrawlRun) WHERE coalesce(c.project_id, '') IN ['', $project_id] RETURN count(c)",
            {"project_id": project_id}
        )
        crawl_runs = res_crawls.get_next()[0] if res_crawls.has_next() else 0

        res_latest = conn.execute(
            """
            MATCH (latest:CrawlRun)
            WHERE coalesce(latest.project_id, '') IN ['', $project_id]
            RETURN latest
            ORDER BY latest.started_at DESC
            LIMIT 1
            """,
            {"project_id": project_id}
        )
        latest = None
        if res_latest.has_next():
            data = _parse_node(res_latest.get_next()[0])
            latest = CrawlRun(
                id=data.get("id", ""),
                root_steam_id=data.get("root_steam_id", ""),
                max_depth=data.get("max_depth", 2),
                max_nodes=data.get("max_nodes", 2000),
                status=CrawlStatus(data.get("status", "pending")),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                nodes_discovered=data.get("nodes_discovered") or 0,
                edges_discovered=data.get("edges_discovered") or 0,
                private_count=data.get("private_count") or 0,
                error_count=data.get("error_count") or 0,
                message=data.get("message") or "",
                current_depth=data.get("current_depth"),
                current_steam_id=data.get("current_steam_id") or "",
                queue_size=data.get("queue_size") or 0,
                expanded_count=data.get("expanded_count") or 0,
                progress_percent=data.get("progress_percent") or 0,
                last_event=data.get("last_event") or "",
                filtered_count=data.get("filtered_count") or 0,
                friend_count_filtered_count=data.get("friend_count_filtered_count") or 0,
                prior_pool_filtered_count=data.get("prior_pool_filtered_count") or 0,
            )

        return DbStats(
            steam_users=steam_users or 0,
            steam_friend_relationships=relationships or 0,
            crawl_runs=crawl_runs or 0,
            latest_crawl=latest,
        )

    def export_graph(self, project_id: str = "default") -> ExportResponse:
        conn = self._get_conn()
        res_nodes = conn.execute(
            """
            MATCH (n:SteamUser)
            WHERE coalesce(n.project_id, '') IN ['', $project_id]
               OR EXISTS { MATCH (n)-[r:STEAM_FRIEND]-(:SteamUser) WHERE r.project_id = $project_id }
            RETURN n
            """,
            {"project_id": project_id}
        )
        nodes = []
        while res_nodes.has_next():
            nodes.append(_parse_node(res_nodes.get_next()[0]))

        res_edges = conn.execute(
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
            WHERE a.steam_id < b.steam_id AND coalesce(r.project_id, '') IN ['', $project_id]
            RETURN a.steam_id, b.steam_id
            """,
            {"project_id": project_id}
        )
        edges = []
        while res_edges.has_next():
            row = res_edges.get_next()
            edges.append({"source": row[0], "target": row[1]})

        return ExportResponse(nodes=nodes, edges=edges)
