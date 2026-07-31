from __future__ import annotations

import datetime
import gc
import logging
import threading
from collections.abc import Iterable
from pathlib import Path
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

logger = logging.getLogger(__name__)


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
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        buffer_pool_size_bytes = int(buffer_pool_size_gb * 1024 * 1024 * 1024)

        try:
            self.db = kuzu.Database(db_path, buffer_pool_size=buffer_pool_size_bytes)
        except Exception as exc:
            detail = str(exc) or repr(exc)
            lowered = detail.lower()
            if any(term in lowered for term in ["lock", "already in use", "could not set lock", "being used"]):
                hint = (
                    "Kuzu database is already in use. Stop other steam-friend-map/uvicorn "
                    "processes that are using this database, or choose a different KUZU_DB_PATH."
                )
            elif any(term in lowered for term in ["buffer pool", "out of memory", "memory"]):
                hint = (
                    "Kuzu could not open the database because its buffer pool is too small. "
                    "Try lowering the graph query size or increasing KUZU_BUFFER_POOL_SIZE_GB."
                )
            else:
                hint = (
                    "Kuzu could not open the database. No database files were moved, deleted, "
                    "or recreated; your existing project data was left untouched."
                )
            logger.exception("Failed to open Kuzu database at %s", db_path)
            raise RuntimeError(f"{hint} Original error: {detail}") from exc

        self._local = threading.local()
        self._closed = False

    def _get_conn(self) -> kuzu.Connection:
        """获取连接。使用 thread-local 缓存以保证线程安全并重用连接。"""
        if self._closed:
            raise RuntimeError("Kuzu repository is closed")
        if not hasattr(self._local, "conn"):
            self._local.conn = kuzu.Connection(self.db)
        return self._local.conn

    def close(self) -> None:
        """Release Kuzu objects so the embedded database file lock can be reacquired."""
        if self._closed:
            return
        self._closed = True
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            close_conn = getattr(conn, "close", None)
            if callable(close_conn):
                close_conn()
            delattr(self._local, "conn")
        close_db = getattr(self.db, "close", None)
        if callable(close_db):
            close_db()
        self.db = None  # type: ignore[assignment]
        gc.collect()

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

        if "SchemaMigration" not in existing_tables:
            conn.execute("""
                CREATE NODE TABLE SchemaMigration(
                    id STRING,
                    applied_at STRING,
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

        if "IN_PROJECT" not in existing_tables:
            conn.execute("""
                CREATE REL TABLE IN_PROJECT(
                    FROM SteamUser TO Project
                )
            """)

        self._migrate_project_memberships(conn)

    @staticmethod
    def _ensure_project_node(conn: kuzu.Connection, project_id: str) -> None:
        name = "默认项目" if project_id == "default" else project_id
        conn.execute(
            """
            MERGE (p:Project {id: $project_id})
            ON CREATE SET p.name = $name, p.created_at = $now
            """,
            {"project_id": project_id, "name": name, "now": utc_now_iso()},
        )

    def _migrate_project_memberships(self, conn: kuzu.Connection) -> None:
        migration_id = "project-membership-v1"
        existing = conn.execute(
            "MATCH (m:SchemaMigration) WHERE m.id = $id RETURN m.id",
            {"id": migration_id},
        )
        if existing.has_next():
            return

        legacy_project_ids = {"default"}
        user_projects = conn.execute(
            "MATCH (u:SteamUser) RETURN DISTINCT coalesce(u.project_id, '')"
        )
        while user_projects.has_next():
            legacy_project_ids.add(user_projects.get_next()[0] or "default")
        relationship_projects = conn.execute(
            "MATCH ()-[r:STEAM_FRIEND]->() RETURN DISTINCT coalesce(r.project_id, '')"
        )
        while relationship_projects.has_next():
            legacy_project_ids.add(relationship_projects.get_next()[0] or "default")
        for project_id in sorted(legacy_project_ids):
            self._ensure_project_node(conn, project_id)

        conn.execute(
            """
            MATCH (u:SteamUser)
            MATCH (p:Project)
            WHERE p.id = CASE
                WHEN coalesce(u.project_id, '') = '' THEN 'default'
                ELSE u.project_id
            END
            MERGE (u)-[:IN_PROJECT]->(p)
            """
        )
        conn.execute(
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]->(b:SteamUser)
            MATCH (p:Project)
            WHERE p.id = CASE
                WHEN coalesce(r.project_id, '') = '' THEN 'default'
                ELSE r.project_id
            END
            MERGE (a)-[:IN_PROJECT]->(p)
            MERGE (b)-[:IN_PROJECT]->(p)
            """
        )
        conn.execute(
            "MERGE (m:SchemaMigration {id: $id}) SET m.applied_at = $now",
            {"id": migration_id, "now": utc_now_iso()},
        )

    def ensure_default_project(self) -> str:
        return self.create_project(ProjectCreate(name="默认项目"), project_id="default")

    @staticmethod
    def _visible_project_ids(project_id: str) -> list[str]:
        return ["", project_id] if project_id == "default" else [project_id]

    @staticmethod
    def _scalar_count(conn: kuzu.Connection, query: str, params: dict[str, Any]) -> int:
        res = conn.execute(query, params)
        return int(res.get_next()[0] or 0) if res.has_next() else 0

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
            conn.execute("MATCH ()-[r:STEAM_FRIEND]->() WHERE r.project_id = $pid DELETE r", {"pid": project_id})
            conn.execute("MATCH (:SteamUser)-[m:IN_PROJECT]->(p:Project) WHERE p.id = $pid DELETE m", {"pid": project_id})
            conn.execute("MATCH (r:CrawlRun) WHERE r.project_id = $pid DELETE r", {"pid": project_id})
            conn.execute("MATCH (p:Project) WHERE p.id = $pid DELETE p", {"pid": project_id})
            conn.execute(
                """
                MATCH (u:SteamUser)
                WHERE NOT EXISTS { MATCH (u)-[:IN_PROJECT]->(:Project) }
                  AND NOT EXISTS { MATCH (u)-[:STEAM_FRIEND]-(:SteamUser) }
                DELETE u
                """
            )
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
            RETURN p.id, p.name, p.created_at
            ORDER BY p.created_at DESC
            """
        )
        projects = []
        while res.has_next():
            row = res.get_next()
            project_ids = self._visible_project_ids(row[0])
            user_count = self._scalar_count(
                conn,
                "MATCH (:SteamUser)-[:IN_PROJECT]->(p:Project) WHERE p.id = $project_id RETURN count(*)",
                {"project_id": row[0]},
            )
            relationship_count = self._scalar_count(
                conn,
                "MATCH ()-[r:STEAM_FRIEND]->() WHERE coalesce(r.project_id, '') IN $project_ids RETURN count(r)",
                {"project_ids": project_ids},
            )
            crawl_count = self._scalar_count(
                conn,
                "MATCH (c:CrawlRun) WHERE coalesce(c.project_id, '') IN $project_ids RETURN count(c)",
                {"project_ids": project_ids},
            )
            projects.append(ProjectInfo(
                id=row[0],
                name=row[1],
                created_at=row[2],
                steam_users=user_count,
                relationships=relationship_count,
                crawl_runs=crawl_count,
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
        self._ensure_project_node(conn, project_id)
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
                conn.execute(
                    """
                    MATCH (u:SteamUser {steam_id: $steam_id})
                    MATCH (p:Project {id: $project_id})
                    MERGE (u)-[:IN_PROJECT]->(p)
                    """,
                    {"steam_id": row["steam_id"], "project_id": project_id},
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
        self._ensure_project_node(conn, project_id)
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
        conn.execute(
            """
            MATCH (u:SteamUser {steam_id: $steam_id})
            MATCH (p:Project {id: $project_id})
            MERGE (u)-[:IN_PROJECT]->(p)
            """,
            {"steam_id": steam_id, "project_id": project_id},
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
        self._ensure_project_node(conn, project_id)
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
                    MERGE (a)-[r:STEAM_FRIEND {project_id: $project_id}]->(b)
                    ON CREATE SET r.crawl_id = $crawl_id,
                                  r.source_depth = $source_depth
                    """,
                    {
                        "from_id": from_id,
                        "to_id": to_id,
                        "crawl_id": row["crawl_id"],
                        "source_depth": row["source_depth"],
                        "project_id": project_id,
                    }
                )
                conn.execute(
                    """
                    MATCH (a:SteamUser {steam_id: $from_id})
                    MATCH (b:SteamUser {steam_id: $to_id})
                    MATCH (p:Project {id: $project_id})
                    MERGE (a)-[:IN_PROJECT]->(p)
                    MERGE (b)-[:IN_PROJECT]->(p)
                    """,
                    {"from_id": from_id, "to_id": to_id, "project_id": project_id},
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
              AND coalesce(r.project_id, '') IN $project_ids
            RETURN c.steam_id, count(DISTINCT inner)
            """,
            {
                "candidates": candidate_ids,
                "inner_pool": inner_pool_ids,
                "project_ids": self._visible_project_ids(project_id),
            }
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
            friend_count_status=data.get("friend_count_status") or "unknown",
            prior_pool_link_count=data.get("prior_pool_link_count") or 0,
            root_closeness_score=data.get("root_closeness_score") or 0,
            root_route_count=data.get("root_route_count") or 0,
            root_route_total_hops=data.get("root_route_total_hops") or 0,
            root_friend_circle_score=data.get("root_friend_circle_score") or 0,
        )

    def _reachable_ids_from_root(
        self, conn: kuzu.Connection, root: str, depth: int, project_id: str
    ) -> tuple[list[str], bool, int, bool]:
        if depth < 0:
            return [], False, 0, False

        root_res = conn.execute(
            """
            MATCH (r:SteamUser)-[:IN_PROJECT]->(p:Project)
            WHERE r.steam_id = $root AND p.id = $project_id
            RETURN r.steam_id
            """,
            {"root": root, "project_id": project_id},
        )
        if not root_res.has_next():
            return [], False, 0, bool(depth)

        ordered_ids = [root]
        seen = {root}
        frontier = [root]
        reached = 0

        for next_depth in range(1, depth + 1):
            if not frontier:
                break
            res = conn.execute(
                """
                MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                WHERE a.steam_id IN $frontier
                  AND coalesce(r.project_id, '') IN $project_ids
                RETURN DISTINCT b.steam_id
                """,
                {"frontier": frontier, "project_ids": self._visible_project_ids(project_id)},
            )
            next_frontier = []
            while res.has_next():
                candidate = res.get_next()[0]
                if candidate in seen:
                    continue
                seen.add(candidate)
                ordered_ids.append(candidate)
                next_frontier.append(candidate)
            if not next_frontier:
                break
            frontier = next_frontier
            reached = next_depth

        return ordered_ids, True, reached, reached < depth

    def _root_friend_circle_metrics(
        self,
        conn: kuzu.Connection,
        root: str,
        reachable_ids: list[str],
        target_ids: list[str],
        depth: int,
        project_id: str,
        route_cap: int = 200,
    ) -> dict[str, tuple[int, int, float]]:
        if not reachable_ids or not target_ids:
            return {}
        target_set = set(target_ids)

        res = conn.execute(
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
            WHERE a.steam_id IN $ids AND b.steam_id IN $ids
              AND coalesce(r.project_id, '') IN $project_ids
            RETURN a.steam_id, b.steam_id
            """,
            {"ids": reachable_ids, "project_ids": self._visible_project_ids(project_id)},
        )
        adjacency = {steam_id: set() for steam_id in reachable_ids}
        while res.has_next():
            source, target = res.get_next()
            if source == target:
                continue
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)

        neighbors_by_id = {
            steam_id: sorted(neighbors)
            for steam_id, neighbors in adjacency.items()
        }
        route_counts = {steam_id: 0 for steam_id in target_set}
        total_hops = {steam_id: 0 for steam_id in target_set}
        capped_targets = 0
        max_capped_targets = max(0, len(target_set - {root}))

        def walk(current: str, remaining_depth: int, path: set[str], hops: int) -> None:
            nonlocal capped_targets
            if remaining_depth <= 0:
                return
            for neighbor in neighbors_by_id.get(current, ()):
                if capped_targets >= max_capped_targets:
                    return
                if neighbor in path:
                    continue
                if neighbor in target_set and route_counts.get(neighbor, 0) < route_cap:
                    route_counts[neighbor] += 1
                    total_hops[neighbor] += hops + 1
                    if neighbor != root and route_counts[neighbor] == route_cap:
                        capped_targets += 1
                if remaining_depth > 1:
                    path.add(neighbor)
                    walk(neighbor, remaining_depth - 1, path, hops + 1)
                    path.remove(neighbor)

        walk(root, depth, {root}, 0)

        metrics: dict[str, tuple[int, int, float]] = {}
        for steam_id in target_ids:
            if steam_id == root:
                metrics[steam_id] = (1, 0, 1_000_000.0)
                continue
            count = min(route_counts.get(steam_id, 0), route_cap)
            hops = total_hops.get(steam_id, 0)
            score = float(count * 1000 - hops) if count else 0.0
            metrics[steam_id] = (count, hops, score)
        return metrics

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
        limit = max(1, min(limit, 100000))
        filters = []
        params: dict[str, Any] = {"project_ids": self._visible_project_ids(project_id)}
        requested_depth: int | None = depth
        traversal_depth_reached: int | None = None
        root_found: bool | None = None
        depth_incomplete = False

        if not root:
            filters.append("p.id = $project_id")
            params["project_id"] = project_id
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
        root_filter = " AND " + " AND ".join(filters) if filters else ""
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
        root_metrics: dict[str, tuple[int, int, float]] = {}
        if root:
            reachable_ids, root_found, traversal_depth_reached, depth_incomplete = self._reachable_ids_from_root(conn, root, depth, project_id)
            if not root_found or not reachable_ids:
                return GraphResponse(
                    nodes=[],
                    edges=[],
                    requested_depth=requested_depth,
                    traversal_depth_reached=traversal_depth_reached,
                    root_found=root_found,
                    depth_incomplete=depth_incomplete,
                )
            params["reachable_ids"] = reachable_ids
            node_query = f"""
            MATCH (n:SteamUser)
            WHERE n.steam_id IN $reachable_ids
            {root_filter}
            OPTIONAL MATCH (n)-[rel:STEAM_FRIEND]-() WHERE coalesce(rel.project_id, '') IN $project_ids
            WITH n, count(DISTINCT rel) AS degree
            RETURN n, degree
            ORDER BY {order_expr} {direction}, degree DESC
            LIMIT $limit
            """
            params["limit"] = limit + 1
        else:
            params["limit"] = limit + 1
            node_query = f"""
            MATCH (n:SteamUser)-[:IN_PROJECT]->(p:Project)
            {where}
            OPTIONAL MATCH (n)-[rel:STEAM_FRIEND]-()
            WHERE coalesce(rel.project_id, '') IN $project_ids
            WITH n, count(DISTINCT rel) AS degree
            RETURN n, degree
            ORDER BY {order_expr} {direction}, degree DESC
            LIMIT $limit
            """

        res_nodes = conn.execute(node_query, params)
        records = []
        while res_nodes.has_next():
            records.append(res_nodes.get_next())
        limited = len(records) > limit
        records = records[:limit]
        nodes = [self._graph_node(_parse_node(rec[0]), rec[1]) for rec in records]
        if root and nodes:
            root_metrics = self._root_friend_circle_metrics(
                conn,
                root,
                reachable_ids,
                [node.id for node in nodes],
                depth,
                project_id,
            )
            for node in nodes:
                route_count, total_hops, score = root_metrics.get(node.id, (0, 0, 0.0))
                node.root_route_count = route_count
                node.root_route_total_hops = total_hops
                node.root_friend_circle_score = score
        ids = [node.id for node in nodes]

        if not ids:
            return GraphResponse(
                nodes=[],
                edges=[],
                requested_depth=requested_depth,
                traversal_depth_reached=traversal_depth_reached,
                root_found=root_found,
                depth_incomplete=depth_incomplete,
            )

        res_edges = conn.execute(
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
            WHERE a.steam_id IN $ids AND b.steam_id IN $ids AND a.steam_id < b.steam_id
              AND coalesce(r.project_id, '') IN $project_ids
            RETURN a.steam_id, b.steam_id
            LIMIT 5000
            """,
            {"ids": ids, "project_ids": self._visible_project_ids(project_id)}
        )

        edges = []
        while res_edges.has_next():
            row = res_edges.get_next()
            edges.append(GraphEdge(
                id=f"{row[0]}-{row[1]}",
                source=row[0],
                target=row[1],
                strength=1
            ))
        return GraphResponse(
            nodes=nodes,
            edges=edges,
            limited=limited,
            requested_depth=requested_depth,
            traversal_depth_reached=traversal_depth_reached,
            root_found=root_found,
            depth_incomplete=depth_incomplete,
        )

    def get_shortest_path(
        self, from_id: str, to_id: str, max_depth: int, project_id: str = "default"
    ) -> GraphResponse:
        max_depth = max(0, min(max_depth, 4))
        conn = self._get_conn()
        res = conn.execute(
            f"""
            WITH $project_ids AS project_ids, $from_id AS fid, $to_id AS tid
            MATCH p=(a:SteamUser {{steam_id: fid}})-[:STEAM_FRIEND*..{max_depth}]-(b:SteamUser {{steam_id: tid}})
            WHERE all(r IN relationships(p) WHERE coalesce(r.project_id, '') IN project_ids)
            RETURN nodes(p)
            ORDER BY length(p) ASC
            LIMIT 1
            """,
            {
                "from_id": from_id,
                "to_id": to_id,
                "project_ids": self._visible_project_ids(project_id),
            }
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
            WITH $project_ids AS project_ids, $root AS root_id
            MATCH (root:SteamUser {{steam_id: root_id}})
            MATCH p=(root)-[:STEAM_FRIEND*2..{max_depth}]-(candidate:SteamUser)
            WHERE all(r IN relationships(p) WHERE coalesce(r.project_id, '') IN project_ids)
            WITH root, candidate, min(length(p)) AS depth, project_ids
            WHERE candidate.steam_id <> root.steam_id
              AND NOT EXISTS {{
                MATCH (root)-[direct:STEAM_FRIEND]-(candidate)
                WHERE coalesce(direct.project_id, '') IN project_ids
              }}
            MATCH (candidate)-[evidence_rel:STEAM_FRIEND]-(evidence:SteamUser)
            WHERE coalesce(evidence_rel.project_id, '') IN project_ids
              AND (
                coalesce(evidence.depth_min, 999) < coalesce(candidate.depth_min, 999)
                OR EXISTS {{
                  MATCH (root)-[root_rel:STEAM_FRIEND]-(evidence)
                  WHERE coalesce(root_rel.project_id, '') IN project_ids
                }}
              )
            WITH candidate,
                 depth,
                 collect(DISTINCT evidence) AS all_evidence,
                 count(DISTINCT evidence) AS mutual_count,
                 project_ids
            WHERE mutual_count >= $min_mutual
            OPTIONAL MATCH (candidate)-[rel:STEAM_FRIEND]-()
            WHERE coalesce(rel.project_id, '') IN project_ids
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
                "project_ids": self._visible_project_ids(project_id),
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
            MATCH (n:SteamUser)-[r:STEAM_FRIEND]-()
            WHERE coalesce(r.project_id, '') IN $project_ids
            WITH n, count(DISTINCT r) AS degree
            RETURN n, degree
            ORDER BY degree DESC
            LIMIT $limit
            """,
            {"project_ids": self._visible_project_ids(project_id), "limit": limit}
        )
        nodes = []
        while res.has_next():
            row = res.get_next()
            node_dict = _parse_node(row[0])
            nodes.append(self._graph_node(node_dict, row[1]))
        return nodes

    def get_db_stats(self, project_id: str = "default") -> DbStats:
        conn = self._get_conn()
        project_ids = self._visible_project_ids(project_id)
        steam_users = self._scalar_count(
            conn,
            "MATCH (:SteamUser)-[:IN_PROJECT]->(p:Project) WHERE p.id = $project_id RETURN count(*)",
            {"project_id": project_id},
        )
        relationships = self._scalar_count(
            conn,
            "MATCH ()-[r:STEAM_FRIEND]->() WHERE coalesce(r.project_id, '') IN $project_ids RETURN count(r)",
            {"project_ids": project_ids},
        )
        crawl_runs = self._scalar_count(
            conn,
            "MATCH (c:CrawlRun) WHERE coalesce(c.project_id, '') IN $project_ids RETURN count(c)",
            {"project_ids": project_ids},
        )

        res_latest = conn.execute(
            """
            MATCH (latest:CrawlRun)
            WHERE coalesce(latest.project_id, '') IN $project_ids
            RETURN latest
            ORDER BY latest.started_at DESC
            LIMIT 1
            """,
            {"project_ids": project_ids}
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
            MATCH (n:SteamUser)-[:IN_PROJECT]->(p:Project)
            WHERE p.id = $project_id
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
            WHERE a.steam_id < b.steam_id AND coalesce(r.project_id, '') IN $project_ids
            RETURN a.steam_id, b.steam_id
            """,
            {"project_ids": self._visible_project_ids(project_id)}
        )
        edges = []
        while res_edges.has_next():
            row = res_edges.get_next()
            edges.append({"source": row[0], "target": row[1]})

        return ExportResponse(nodes=nodes, edges=edges)
