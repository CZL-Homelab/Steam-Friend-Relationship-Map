from __future__ import annotations

import datetime
import gc
import logging
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import kuzu

from .graph_repo import IGraphRepository, validate_crawl_run_update_fields
from .models import (
    CrawlRun,
    CrawlStatus,
    DbStats,
    ExportResponse,
    FriendCircleAnalysisResponse,
    FriendCircleCandidate,
    FriendEdge,
    FriendListCacheUpdate,
    GraphEdge,
    GraphNode,
    GraphResponse,
    PotentialFriendCandidate,
    PotentialFriendsResponse,
    ProjectCreate,
    ProjectInfo,
    ProjectListResponse,
    SteamUserRecord,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

_PROJECT_MEMBER_PROPERTY_TYPES = {
    "depth_min": "INT64",
    "prior_pool_link_count": "INT64",
    "root_closeness_score": "DOUBLE",
    "last_scored_crawl_id": "STRING",
    "note": "STRING",
    "tags": "STRING[]",
    "category": "STRING",
}
_KUZU_WRITE_BATCH_SIZE = 500


def _recovery_backup_hint(db_path: str, error_detail: str) -> str:
    """Describe legacy auto-recovery leftovers without changing database files."""
    if "invalid unordered_map" not in error_detail.lower():
        return ""

    active_path = Path(db_path)
    wal_path = Path(f"{db_path}.wal")
    try:
        candidates = [
            path
            for path in active_path.parent.glob(f"{active_path.name}_corrupted_*")
            if path.is_file()
        ]
        if not candidates or not active_path.is_file() or not wal_path.is_file():
            return ""
        backup_path = max(candidates, key=lambda path: (path.stat().st_size, path.stat().st_mtime))
        active_size = active_path.stat().st_size
        backup_size = backup_path.stat().st_size
    except OSError:
        return ""

    if backup_size <= active_size:
        return ""
    return (
        " The active database and WAL may not belong together: a larger legacy "
        f"auto-recovery backup exists at '{backup_path}' ({backup_size} bytes), while "
        f"the active database is only {active_size} bytes. Recover from verified copies "
        "of the backup and WAL; do not delete or overwrite the originals."
    )


def _parse_node(row_val: Any) -> dict[str, Any]:
    """将 Kùzu 查询返回的节点转换为标准的 Python 字典。"""
    if isinstance(row_val, dict):
        return row_val
    if hasattr(row_val, "properties") and isinstance(row_val.properties, dict):
        return row_val.properties
    return {}


def _iter_rows(result: Any) -> Iterator[list[Any]]:
    """Iterate a Kuzu result and close it once the rows are consumed."""
    try:
        while result.has_next():
            yield result.get_next()
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()


def _consume_rows(result: Any) -> list[list[Any]]:
    """Materialize a small Kuzu result while preserving result ownership."""
    return list(_iter_rows(result))


def _execute_discard(
    conn: kuzu.Connection,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> None:
    """Execute a statement and promptly release its unused Kuzu result."""
    result = conn.execute(query, parameters) if parameters is not None else conn.execute(query)
    close = getattr(result, "close", None)
    if callable(close):
        close()


def _rollback_after_error(conn: kuzu.Connection) -> None:
    try:
        _execute_discard(conn, "ROLLBACK")
    except Exception:
        logger.warning("Kuzu transaction rollback failed", exc_info=True)


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
            recovery_hint = _recovery_backup_hint(db_path, detail)
            if recovery_hint:
                hint = (
                    "Kuzu could not open the database because its storage files appear "
                    "to be inconsistent. No database files were moved, deleted, or recreated."
                    f"{recovery_hint}"
                )
            elif any(
                term in lowered
                for term in [
                    "lock",
                    "already in use",
                    "could not set lock",
                    "being used",
                ]
            ):
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
        self._connections_lock = threading.Lock()
        self._connections: dict[int, kuzu.Connection] = {}
        self._closed = False

    def _get_conn(self) -> kuzu.Connection:
        """获取连接。使用 thread-local 缓存以保证线程安全并重用连接。"""
        if self._closed:
            raise RuntimeError("Kuzu repository is closed")
        if not hasattr(self._local, "conn"):
            with self._connections_lock:
                if self._closed:
                    raise RuntimeError("Kuzu repository is closed")
                conn = kuzu.Connection(self.db)
                self._connections[id(conn)] = conn
                self._local.conn = conn
        return self._local.conn

    def close(self) -> None:
        """Release Kuzu objects so the embedded database file lock can be reacquired."""
        with self._connections_lock:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections.values())
            self._connections.clear()
        first_error: Exception | None = None
        for conn in connections:
            close_conn = getattr(conn, "close", None)
            if callable(close_conn):
                try:
                    close_conn()
                except Exception as exc:
                    first_error = first_error or exc
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")
        close_db = getattr(self.db, "close", None)
        if callable(close_db):
            try:
                close_db()
            except Exception as exc:
                first_error = first_error or exc
        self.db = None  # type: ignore[assignment]
        gc.collect()
        if first_error is not None:
            raise first_error

    def test_connection(self) -> str:
        conn = self._get_conn()
        if _consume_rows(conn.execute("RETURN 1")):
            return "Kùzu 连接正常"
        raise RuntimeError("Kùzu 连接异常")

    def recover_interrupted_crawls(self) -> int:
        conn = self._get_conn()
        statuses = [
            CrawlStatus.pending.value,
            CrawlStatus.running.value,
            CrawlStatus.paused.value,
        ]
        interrupted = self._scalar_count(
            conn,
            "MATCH (c:CrawlRun) WHERE c.status IN $statuses RETURN count(c)",
            {"statuses": statuses},
        )
        if not interrupted:
            return 0
        message = "应用重启前抓取未正常结束"
        _consume_rows(
            conn.execute(
                """
                MATCH (c:CrawlRun)
                WHERE c.status IN $statuses
                SET c.status = $status,
                    c.finished_at = $finished_at,
                    c.message = $message,
                    c.last_event = $message
                """,
                {
                    "statuses": statuses,
                    "status": CrawlStatus.stopped.value,
                    "finished_at": utc_now_iso(),
                    "message": message,
                },
            )
        )
        return interrupted

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        existing_tables = {
            row[0] for row in _consume_rows(conn.execute("CALL show_tables() RETURN name"))
        }

        if "SteamUser" not in existing_tables:
            _execute_discard(
                conn,
                """
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
            """,
            )

        if "CrawlRun" not in existing_tables:
            _execute_discard(
                conn,
                """
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
            """,
            )

        if "Project" not in existing_tables:
            _execute_discard(
                conn,
                """
                CREATE NODE TABLE Project(
                    id STRING,
                    name STRING,
                    created_at STRING,
                    PRIMARY KEY(id)
                )
            """,
            )

        if "SchemaMigration" not in existing_tables:
            _execute_discard(
                conn,
                """
                CREATE NODE TABLE SchemaMigration(
                    id STRING,
                    applied_at STRING,
                    PRIMARY KEY(id)
                )
            """,
            )

        if "STEAM_FRIEND" not in existing_tables:
            _execute_discard(
                conn,
                """
                CREATE REL TABLE STEAM_FRIEND(
                    FROM SteamUser TO SteamUser,
                    crawl_id STRING,
                    source_depth INT64,
                    project_id STRING
                )
            """,
            )

        if "IN_PROJECT" not in existing_tables:
            _execute_discard(
                conn,
                """
                CREATE REL TABLE IN_PROJECT(
                    FROM SteamUser TO Project,
                    depth_min INT64,
                    prior_pool_link_count INT64,
                    root_closeness_score DOUBLE,
                    last_scored_crawl_id STRING,
                    note STRING,
                    tags STRING[],
                    category STRING
                )
            """,
            )
        else:
            existing_properties = {
                row[0]
                for row in _consume_rows(conn.execute("CALL table_info('IN_PROJECT') RETURN name"))
            }
            for name, property_type in _PROJECT_MEMBER_PROPERTY_TYPES.items():
                if name not in existing_properties:
                    _execute_discard(conn, f"ALTER TABLE IN_PROJECT ADD {name} {property_type}")

        self._migrate_project_memberships(conn)
        self._migrate_project_member_metadata(conn)

    @staticmethod
    def _ensure_project_node(conn: kuzu.Connection, project_id: str) -> None:
        name = "默认项目" if project_id == "default" else project_id
        _execute_discard(
            conn,
            """
            MERGE (p:Project {id: $project_id})
            ON CREATE SET p.name = $name, p.created_at = $now
            """,
            {"project_id": project_id, "name": name, "now": utc_now_iso()},
        )

    def _migrate_project_memberships(self, conn: kuzu.Connection) -> None:
        migration_id = "project-membership-v1"
        existing = _consume_rows(
            conn.execute(
                "MATCH (m:SchemaMigration) WHERE m.id = $id RETURN m.id",
                {"id": migration_id},
            )
        )
        if existing:
            return

        legacy_project_ids = {"default"}
        for row in _consume_rows(
            conn.execute("MATCH (u:SteamUser) RETURN DISTINCT coalesce(u.project_id, '')")
        ):
            legacy_project_ids.add(row[0] or "default")
        for row in _iter_rows(
            conn.execute("MATCH ()-[r:STEAM_FRIEND]->() RETURN DISTINCT coalesce(r.project_id, '')")
        ):
            legacy_project_ids.add(row[0] or "default")
        for project_id in sorted(legacy_project_ids):
            self._ensure_project_node(conn, project_id)

        _execute_discard(
            conn,
            """
            MATCH (u:SteamUser)
            MATCH (p:Project)
            WHERE p.id = CASE
                WHEN coalesce(u.project_id, '') = '' THEN 'default'
                ELSE u.project_id
            END
            MERGE (u)-[:IN_PROJECT]->(p)
            """,
        )
        _execute_discard(
            conn,
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]->(b:SteamUser)
            MATCH (p:Project)
            WHERE p.id = CASE
                WHEN coalesce(r.project_id, '') = '' THEN 'default'
                ELSE r.project_id
            END
            MERGE (a)-[:IN_PROJECT]->(p)
            MERGE (b)-[:IN_PROJECT]->(p)
            """,
        )
        _execute_discard(
            conn,
            "MERGE (m:SchemaMigration {id: $id}) SET m.applied_at = $now",
            {"id": migration_id, "now": utc_now_iso()},
        )

    @staticmethod
    def _migrate_project_member_metadata(conn: kuzu.Connection) -> None:
        migration_id = "project-member-metadata-v2"
        existing = _consume_rows(
            conn.execute(
                "MATCH (m:SchemaMigration) WHERE m.id = $id RETURN m.id",
                {"id": migration_id},
            )
        )
        if existing:
            return

        # Legacy node properties belonged to the user's original project. Copy them
        # once, then use IN_PROJECT exclusively for project-specific metadata.
        _execute_discard(
            conn,
            """
            MATCH (u:SteamUser)-[membership:IN_PROJECT]->(p:Project)
            WHERE p.id = CASE
                WHEN coalesce(u.project_id, '') = '' THEN 'default'
                ELSE u.project_id
            END
            SET membership.depth_min = u.depth_min,
                membership.prior_pool_link_count = coalesce(u.prior_pool_link_count, 0),
                membership.root_closeness_score = coalesce(u.root_closeness_score, 0.0),
                membership.last_scored_crawl_id = coalesce(u.last_scored_crawl_id, ''),
                membership.note = coalesce(u.note, ''),
                membership.tags = coalesce(u.tags, CAST([] AS STRING[])),
                membership.category = coalesce(u.category, '')
            """,
        )
        _execute_discard(
            conn,
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
        rows = _consume_rows(conn.execute(query, params))
        return int(rows[0][0] or 0) if rows else 0

    def create_project(self, payload: ProjectCreate, project_id: str | None = None) -> str:
        import uuid

        pid = project_id or str(uuid.uuid4())
        now = utc_now_iso()
        conn = self._get_conn()
        _execute_discard(
            conn,
            """
            MERGE (p:Project {id: $pid})
            ON CREATE SET p.name = $name, p.created_at = $now
            ON MATCH SET p.name = $name
            """,
            {"pid": pid, "name": payload.name, "now": now},
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
            _execute_discard(conn, "BEGIN TRANSACTION")
            _execute_discard(
                conn,
                "MATCH ()-[r:STEAM_FRIEND]->() WHERE r.project_id = $pid DELETE r",
                {"pid": project_id},
            )
            _execute_discard(
                conn,
                "MATCH (:SteamUser)-[m:IN_PROJECT]->(p:Project) WHERE p.id = $pid DELETE m",
                {"pid": project_id},
            )
            _execute_discard(
                conn,
                "MATCH (r:CrawlRun) WHERE r.project_id = $pid DELETE r",
                {"pid": project_id},
            )
            _execute_discard(
                conn,
                "MATCH (p:Project) WHERE p.id = $pid DELETE p",
                {"pid": project_id},
            )
            _execute_discard(
                conn,
                """
                MATCH (u:SteamUser)
                WHERE NOT EXISTS { MATCH (u)-[:IN_PROJECT]->(:Project) }
                  AND NOT EXISTS { MATCH (u)-[:STEAM_FRIEND]-(:SteamUser) }
                DELETE u
                """,
            )
            _execute_discard(conn, "COMMIT")
            return True
        except Exception:
            _rollback_after_error(conn)
            raise

    def project_exists(self, project_id: str) -> bool:
        conn = self._get_conn()
        rows = _consume_rows(
            conn.execute(
                "MATCH (p:Project) WHERE p.id = $pid RETURN p.id",
                {"pid": project_id},
            )
        )
        return bool(rows)

    def list_projects(self) -> ProjectListResponse:
        conn = self._get_conn()
        project_rows = [
            (project_id, name, created_at)
            for project_id, name, created_at in _consume_rows(
                conn.execute(
                    """
                    MATCH (p:Project)
                    RETURN p.id, p.name, p.created_at
                    ORDER BY p.created_at DESC
                    """
                )
            )
        ]
        if not project_rows:
            self.ensure_schema()
            self.ensure_default_project()
            return ProjectListResponse(
                projects=[ProjectInfo(id="default", name="默认项目", created_at=utc_now_iso())],
                active_project_id="",
            )

        user_counts: dict[str, int] = {}
        for project_id, count in _consume_rows(
            conn.execute(
                """
                MATCH (:SteamUser)-[:IN_PROJECT]->(p:Project)
                RETURN p.id, count(*)
                """
            )
        ):
            user_counts[project_id] = int(count or 0)

        relationship_counts: dict[str, int] = {}
        for project_id, count in _consume_rows(
            conn.execute(
                """
                MATCH ()-[r:STEAM_FRIEND]->()
                WITH CASE
                    WHEN coalesce(r.project_id, '') = '' THEN 'default'
                    ELSE r.project_id
                END AS project_id
                RETURN project_id, count(*)
                """
            )
        ):
            relationship_counts[project_id] = int(count or 0)

        crawl_counts: dict[str, int] = {}
        for project_id, count in _consume_rows(
            conn.execute(
                """
                MATCH (c:CrawlRun)
                WITH CASE
                    WHEN coalesce(c.project_id, '') = '' THEN 'default'
                    ELSE c.project_id
                END AS project_id
                RETURN project_id, count(*)
                """
            )
        ):
            crawl_counts[project_id] = int(count or 0)

        projects = [
            ProjectInfo(
                id=project_id,
                name=name,
                created_at=created_at,
                steam_users=user_counts.get(project_id, 0),
                relationships=relationship_counts.get(project_id, 0),
                crawl_runs=crawl_counts.get(project_id, 0),
            )
            for project_id, name, created_at in project_rows
        ]
        return ProjectListResponse(projects=projects, active_project_id="")

    def get_crawl_run(self, run_id: str) -> CrawlRun | None:
        conn = self._get_conn()
        rows = _consume_rows(
            conn.execute(
                """
                MATCH (r:CrawlRun)
                WHERE r.id = $run_id
                RETURN r
                """,
                {"run_id": run_id},
            )
        )
        if not rows:
            return None
        data = _parse_node(rows[0][0])
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
        _execute_discard(
            conn,
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
            },
        )

    def update_crawl_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        validate_crawl_run_update_fields(fields)
        assignments = ", ".join(f"r.{key} = ${key}" for key in fields)
        conn = self._get_conn()
        _execute_discard(
            conn,
            f"MATCH (r:CrawlRun {{id: $run_id}}) SET {assignments}",
            {"run_id": run_id, **fields},
        )

    def upsert_users(self, users: Iterable[SteamUserRecord], project_id: str) -> None:
        rows = [user.model_dump(mode="json") for user in users]
        if not rows:
            return
        now = utc_now_iso()
        conn = self._get_conn()
        self._ensure_project_node(conn, project_id)
        query = """
            MATCH (p:Project {id: $project_id})
            UNWIND $rows AS row
            MERGE (u:SteamUser {steam_id: row.steam_id})
            ON CREATE SET u.first_seen_at = $now,
                          u.friend_ids = CAST([] AS STRING[]),
                          u.friend_list_fetched_at = ''
            SET u.last_seen_at = $now,
                u.project_id = CASE
                    WHEN u.project_id IS NULL OR u.project_id = '' THEN $project_id
                    ELSE u.project_id
                END,
                u.persona_name = row.persona_name,
                u.profile_url = row.profile_url,
                u.avatar = row.avatar,
                u.avatar_medium = row.avatar_medium,
                u.avatar_full = row.avatar_full,
                u.visibility_state = CAST(row.visibility_state AS INT64),
                u.profile_state = CAST(row.profile_state AS INT64),
                u.friend_count = CASE
                    WHEN row.friend_count IS NULL THEN u.friend_count
                    ELSE CAST(row.friend_count AS INT64)
                END,
                u.friend_count_status = CASE
                    WHEN row.friend_count_status IS NULL OR row.friend_count_status = 'unknown' THEN coalesce(u.friend_count_status, 'unknown')
                    ELSE row.friend_count_status
                END,
                u.friend_list_status = CASE
                    WHEN row.friend_list_status = 'unknown' THEN coalesce(u.friend_list_status, 'unknown')
                    WHEN coalesce(u.friend_list_status, 'unknown') = 'private' THEN 'private'
                    ELSE row.friend_list_status
                END
            MERGE (u)-[membership:IN_PROJECT]->(p)
            SET membership.depth_min = CASE
                    WHEN membership.depth_min IS NULL OR row.depth_min < membership.depth_min THEN row.depth_min
                    ELSE membership.depth_min
                END,
                membership.prior_pool_link_count = CASE
                    WHEN row.prior_pool_link_count > coalesce(membership.prior_pool_link_count, 0) THEN row.prior_pool_link_count
                    ELSE coalesce(membership.prior_pool_link_count, 0)
                END,
                membership.root_closeness_score = CASE
                    WHEN row.root_closeness_score > coalesce(membership.root_closeness_score, 0.0) THEN row.root_closeness_score
                    ELSE coalesce(membership.root_closeness_score, 0.0)
                END,
                membership.last_scored_crawl_id = CASE
                    WHEN row.last_scored_crawl_id = '' THEN coalesce(membership.last_scored_crawl_id, '')
                    ELSE row.last_scored_crawl_id
                END,
                membership.note = coalesce(membership.note, ''),
                membership.tags = coalesce(membership.tags, CAST([] AS STRING[])),
                membership.category = coalesce(membership.category, '')
        """
        try:
            _execute_discard(conn, "BEGIN TRANSACTION")
            for offset in range(0, len(rows), _KUZU_WRITE_BATCH_SIZE):
                _execute_discard(
                    conn,
                    query,
                    {
                        "now": now,
                        "project_id": project_id,
                        "rows": rows[offset : offset + _KUZU_WRITE_BATCH_SIZE],
                    },
                )
            _execute_discard(conn, "COMMIT")
        except Exception:
            _rollback_after_error(conn)
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
        self.mark_friend_list_statuses(
            [
                FriendListCacheUpdate(
                    steam_id=steam_id,
                    status=status,
                    friend_count=friend_count,
                    friend_count_status=friend_count_status,
                    friend_ids=friend_ids,
                )
            ],
            project_id,
        )

    def mark_friend_list_statuses(
        self, updates: Iterable[FriendListCacheUpdate], project_id: str
    ) -> None:
        rows = [update.model_dump(mode="json") for update in updates]
        if not rows:
            return
        now = utc_now_iso()
        conn = self._get_conn()
        self._ensure_project_node(conn, project_id)
        query = """
            MATCH (p:Project {id: $project_id})
            UNWIND $rows AS row
            MERGE (u:SteamUser {steam_id: row.steam_id})
            ON CREATE SET u.first_seen_at = $now,
                          u.friend_ids = CAST([] AS STRING[])
            SET u.friend_list_status = row.status,
                u.project_id = CASE
                    WHEN u.project_id IS NULL OR u.project_id = '' THEN $project_id
                    ELSE u.project_id
                END,
                u.friend_count = CASE
                    WHEN row.friend_count IS NULL THEN u.friend_count
                    ELSE CAST(row.friend_count AS INT64)
                END,
                u.friend_count_status = CASE
                    WHEN row.friend_count_status IS NULL THEN coalesce(u.friend_count_status, 'unknown')
                    ELSE row.friend_count_status
                END,
                u.friend_ids = CASE
                    WHEN row.friend_ids IS NULL THEN u.friend_ids
                    ELSE CAST(row.friend_ids AS STRING[])
                END,
                u.friend_list_fetched_at = $now,
                u.last_seen_at = $now
            MERGE (u)-[membership:IN_PROJECT]->(p)
            SET membership.note = coalesce(membership.note, ''),
                membership.tags = coalesce(membership.tags, CAST([] AS STRING[])),
                membership.category = coalesce(membership.category, ''),
                membership.prior_pool_link_count = coalesce(membership.prior_pool_link_count, 0),
                membership.root_closeness_score = coalesce(membership.root_closeness_score, 0.0),
                membership.last_scored_crawl_id = coalesce(membership.last_scored_crawl_id, '')
        """
        try:
            _execute_discard(conn, "BEGIN TRANSACTION")
            for offset in range(0, len(rows), _KUZU_WRITE_BATCH_SIZE):
                _execute_discard(
                    conn,
                    query,
                    {
                        "rows": rows[offset : offset + _KUZU_WRITE_BATCH_SIZE],
                        "project_id": project_id,
                        "now": now,
                    },
                )
            _execute_discard(conn, "COMMIT")
        except Exception:
            _rollback_after_error(conn)
            raise

    def get_cached_friend_list(
        self, steam_id: str, valid_days: int, project_id: str
    ) -> tuple[str, list[str]] | None:
        return self.get_cached_friend_lists([steam_id], valid_days, project_id).get(steam_id)

    def get_cached_friend_lists(
        self, steam_ids: Iterable[str], valid_days: int, project_id: str
    ) -> dict[str, tuple[str, list[str]]]:
        unique_ids = list(dict.fromkeys(steam_ids))
        if valid_days <= 0 or not unique_ids:
            return {}
        cutoff_time = (
            (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=valid_days))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        conn = self._get_conn()
        cached_lists: dict[str, tuple[str, list[str]]] = {}
        for steam_id, raw_status, raw_friend_ids in _consume_rows(
            conn.execute(
                """
                MATCH (u:SteamUser)
                WHERE u.steam_id IN $steam_ids AND u.friend_list_fetched_at >= $cutoff_time
                RETURN u.steam_id, u.friend_list_status, u.friend_ids
                """,
                {"steam_ids": unique_ids, "cutoff_time": cutoff_time},
            )
        ):
            status = raw_status or "unknown"
            if status == "unknown":
                continue
            if status != "public":
                cached_lists[steam_id] = (status, [])
                continue
            if raw_friend_ids is None:
                continue
            cached_lists[steam_id] = (status, list(raw_friend_ids))
        return cached_lists

    def upsert_relationships(self, edges: Iterable[FriendEdge], project_id: str) -> None:
        normalized: dict[tuple[str, str], dict[str, Any]] = {}
        for edge in edges:
            row = edge.model_dump(mode="json")
            from_id = row["from_id"]
            to_id = row["to_id"]
            if from_id == to_id:
                continue
            if from_id > to_id:
                from_id, to_id = to_id, from_id
            normalized.setdefault(
                (from_id, to_id),
                {
                    "from_id": from_id,
                    "to_id": to_id,
                    "crawl_id": row["crawl_id"],
                    "source_depth": row["source_depth"],
                },
            )
        rows = list(normalized.values())
        if not rows:
            return
        conn = self._get_conn()
        self._ensure_project_node(conn, project_id)
        query = """
            MATCH (p:Project {id: $project_id})
            UNWIND $rows AS row
            MATCH (a:SteamUser {steam_id: row.from_id})
            MATCH (b:SteamUser {steam_id: row.to_id})
            MERGE (a)-[r:STEAM_FRIEND {project_id: $project_id}]->(b)
            ON CREATE SET r.crawl_id = row.crawl_id,
                          r.source_depth = row.source_depth
            MERGE (a)-[a_membership:IN_PROJECT]->(p)
            MERGE (b)-[b_membership:IN_PROJECT]->(p)
            SET a_membership.note = coalesce(a_membership.note, ''),
                a_membership.tags = coalesce(a_membership.tags, CAST([] AS STRING[])),
                a_membership.category = coalesce(a_membership.category, ''),
                a_membership.prior_pool_link_count = coalesce(a_membership.prior_pool_link_count, 0),
                a_membership.root_closeness_score = coalesce(a_membership.root_closeness_score, 0.0),
                a_membership.last_scored_crawl_id = coalesce(a_membership.last_scored_crawl_id, ''),
                b_membership.note = coalesce(b_membership.note, ''),
                b_membership.tags = coalesce(b_membership.tags, CAST([] AS STRING[])),
                b_membership.category = coalesce(b_membership.category, ''),
                b_membership.prior_pool_link_count = coalesce(b_membership.prior_pool_link_count, 0),
                b_membership.root_closeness_score = coalesce(b_membership.root_closeness_score, 0.0),
                b_membership.last_scored_crawl_id = coalesce(b_membership.last_scored_crawl_id, '')
        """
        try:
            _execute_discard(conn, "BEGIN TRANSACTION")
            for offset in range(0, len(rows), _KUZU_WRITE_BATCH_SIZE):
                _execute_discard(
                    conn,
                    query,
                    {
                        "project_id": project_id,
                        "rows": rows[offset : offset + _KUZU_WRITE_BATCH_SIZE],
                    },
                )
            _execute_discard(conn, "COMMIT")
        except Exception:
            _rollback_after_error(conn)
            raise

    def patch_user(
        self,
        steam_id: str,
        *,
        note: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
        project_id: str = "default",
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
        assignments = ", ".join(f"membership.{key} = ${key}" for key in fields)
        conn = self._get_conn()
        _execute_discard(
            conn,
            f"""
            MATCH (u:SteamUser)-[membership:IN_PROJECT]->(p:Project)
            WHERE u.steam_id = $steam_id AND p.id = $project_id
            SET {assignments}
            """,
            {"steam_id": steam_id, "project_id": project_id, **fields},
        )

    def bulk_patch_users(
        self, patches: Iterable[dict[str, Any]], project_id: str = "default"
    ) -> None:
        normalized: dict[str, dict[str, Any]] = {}
        for patch in patches:
            steam_id = patch.get("steam_id")
            if not steam_id:
                continue
            fields = {
                key: patch[key]
                for key in ("note", "tags", "category")
                if patch.get(key) is not None
            }
            if fields:
                normalized.setdefault(steam_id, {"steam_id": steam_id}).update(fields)
        if not normalized:
            return

        grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in normalized.values():
            signature = tuple(key for key in ("note", "tags", "category") if key in row)
            grouped.setdefault(signature, []).append(row)

        conn = self._get_conn()
        try:
            _execute_discard(conn, "BEGIN TRANSACTION")
            for signature, rows in grouped.items():
                assignments = ", ".join(
                    f"membership.{key} = "
                    + (f"CAST(row.{key} AS STRING[])" if key == "tags" else f"row.{key}")
                    for key in signature
                )
                query = f"""
                    UNWIND $rows AS row
                    MATCH (u:SteamUser)-[membership:IN_PROJECT]->(p:Project)
                    WHERE u.steam_id = row.steam_id AND p.id = $project_id
                    SET {assignments}
                """
                for offset in range(0, len(rows), _KUZU_WRITE_BATCH_SIZE):
                    _execute_discard(
                        conn,
                        query,
                        {
                            "project_id": project_id,
                            "rows": rows[offset : offset + _KUZU_WRITE_BATCH_SIZE],
                        },
                    )
            _execute_discard(conn, "COMMIT")
        except Exception:
            _rollback_after_error(conn)
            raise

    def count_inner_layer_links(
        self, candidate_ids: list[str], inner_pool_ids: list[str], project_id: str
    ) -> dict[str, int]:
        if not candidate_ids or not inner_pool_ids:
            return {}
        conn = self._get_conn()
        out = {cid: 0 for cid in candidate_ids}
        for row in _consume_rows(
            conn.execute(
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
                },
            )
        ):
            out[row[0]] = row[1]
        return {k: v for k, v in out.items() if v > 0}

    @staticmethod
    def _project_metadata(
        conn: kuzu.Connection, steam_ids: list[str], project_id: str
    ) -> dict[str, dict[str, Any]]:
        if not steam_ids:
            return {}
        result = conn.execute(
            """
            MATCH (u:SteamUser)-[membership:IN_PROJECT]->(p:Project)
            WHERE u.steam_id IN $steam_ids AND p.id = $project_id
            RETURN u.steam_id, membership
            """,
            {"steam_ids": steam_ids, "project_id": project_id},
        )
        metadata: dict[str, dict[str, Any]] = {}
        for steam_id, membership in _consume_rows(result):
            metadata[steam_id] = membership if isinstance(membership, dict) else {}
        return metadata

    @staticmethod
    def _users_by_id(conn: kuzu.Connection, steam_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not steam_ids:
            return {}
        rows = _consume_rows(
            conn.execute(
                "MATCH (u:SteamUser) WHERE u.steam_id IN $steam_ids RETURN u",
                {"steam_ids": list(dict.fromkeys(steam_ids))},
            )
        )
        users = [_parse_node(row[0]) for row in rows]
        return {user.get("steam_id", ""): user for user in users if user.get("steam_id")}

    def _graph_node(
        self, data: dict[str, Any], degree: int, metadata: dict[str, Any] | None = None
    ) -> GraphNode:
        member = metadata or {}
        return GraphNode(
            id=data.get("steam_id", ""),
            label=data.get("persona_name") or data.get("steam_id", "Unknown"),
            depth=member.get("depth_min"),
            avatar=data.get("avatar_full") or data.get("avatar_medium") or data.get("avatar") or "",
            profile_url=data.get("profile_url") or "",
            note=member.get("note") or "",
            tags=member.get("tags") or [],
            category=member.get("category") or "",
            degree=degree,
            friend_count=data.get("friend_count"),
            friend_count_status=data.get("friend_count_status") or "unknown",
            prior_pool_link_count=member.get("prior_pool_link_count") or 0,
            root_closeness_score=member.get("root_closeness_score") or 0,
            root_route_count=data.get("root_route_count") or 0,
            root_route_total_hops=data.get("root_route_total_hops") or 0,
            root_friend_circle_score=data.get("root_friend_circle_score") or 0,
        )

    def _bfs_from_root(
        self, conn: kuzu.Connection, root: str, depth: int, project_id: str
    ) -> tuple[list[str], dict[str, int], dict[str, str], bool, int]:
        if depth < 0:
            return [], {}, {}, False, 0

        root_rows = _consume_rows(
            conn.execute(
                """
                MATCH (r:SteamUser)-[:IN_PROJECT]->(p:Project)
                WHERE r.steam_id = $root AND p.id = $project_id
                RETURN r.steam_id
                """,
                {"root": root, "project_id": project_id},
            )
        )
        if not root_rows:
            return [], {}, {}, False, 0

        ordered_ids = [root]
        seen = {root}
        depths = {root: 0}
        parents: dict[str, str] = {}
        frontier = [root]
        reached = 0

        for next_depth in range(1, depth + 1):
            if not frontier:
                break
            rows = _consume_rows(
                conn.execute(
                    """
                    MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                    WHERE a.steam_id IN $frontier
                      AND coalesce(r.project_id, '') IN $project_ids
                    RETURN DISTINCT a.steam_id, b.steam_id
                    """,
                    {
                        "frontier": frontier,
                        "project_ids": self._visible_project_ids(project_id),
                    },
                )
            )
            next_frontier = []
            for source, candidate in sorted(rows, key=lambda row: (row[1], row[0])):
                if candidate in seen:
                    continue
                seen.add(candidate)
                depths[candidate] = next_depth
                parents[candidate] = source
                ordered_ids.append(candidate)
                next_frontier.append(candidate)
            if not next_frontier:
                break
            frontier = next_frontier
            reached = next_depth

        return ordered_ids, depths, parents, True, reached

    def _reachable_ids_from_root(
        self, conn: kuzu.Connection, root: str, depth: int, project_id: str
    ) -> tuple[list[str], bool, int, bool]:
        ordered_ids, _, _, root_found, reached = self._bfs_from_root(conn, root, depth, project_id)
        return ordered_ids, root_found, reached, bool(root_found and reached < depth)

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
        rows = _consume_rows(
            conn.execute(
                """
                MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                WHERE a.steam_id IN $ids AND b.steam_id IN $ids
                  AND coalesce(r.project_id, '') IN $project_ids
                RETURN a.steam_id, b.steam_id
                """,
                {
                    "ids": reachable_ids,
                    "project_ids": self._visible_project_ids(project_id),
                },
            )
        )
        adjacency = {steam_id: set() for steam_id in reachable_ids}
        for source, target in rows:
            if source == target:
                continue
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)

        neighbors_by_id = {steam_id: sorted(neighbors) for steam_id, neighbors in adjacency.items()}
        metrics: dict[str, tuple[int, int, float]] = {}
        for steam_id in target_ids:
            if steam_id == root:
                metrics[steam_id] = (1, 0, 1_000_000.0)
                continue

            distance_to_target = {steam_id: 0}
            frontier = [steam_id]
            for next_distance in range(1, depth + 1):
                next_frontier = []
                for current in frontier:
                    for neighbor in neighbors_by_id.get(current, ()):
                        if neighbor in distance_to_target:
                            continue
                        distance_to_target[neighbor] = next_distance
                        next_frontier.append(neighbor)
                if not next_frontier:
                    break
                frontier = next_frontier

            count = 0
            hops = 0

            def walk_target(
                current: str, remaining_depth: int, path: set[str], path_hops: int
            ) -> None:
                nonlocal count, hops
                if count >= route_cap or remaining_depth <= 0:
                    return
                for neighbor in neighbors_by_id.get(current, ()):
                    if count >= route_cap:
                        return
                    if neighbor in path:
                        continue
                    next_hops = path_hops + 1
                    if neighbor == steam_id:
                        count += 1
                        hops += next_hops
                        continue
                    if remaining_depth <= 1:
                        continue
                    if distance_to_target.get(neighbor, depth + 1) > remaining_depth - 1:
                        continue
                    path.add(neighbor)
                    walk_target(neighbor, remaining_depth - 1, path, next_hops)
                    path.remove(neighbor)

            if distance_to_target.get(root, depth + 1) <= depth:
                walk_target(root, depth, {root}, 0)
            score = float(count * 1000 - hops) if count else 0.0
            metrics[steam_id] = (count, hops, score)
        return metrics

    def _multi_root_friend_circle_metrics(
        self,
        conn: kuzu.Connection,
        roots: list[str],
        reachable_ids: list[str],
        target_ids: list[str],
        depth: int,
        project_id: str,
        route_cap: int = 200,
    ) -> dict[str, tuple[int, int, float]]:
        root_set = set(roots)
        totals = {target_id: [0, 0] for target_id in target_ids}
        for root_id in sorted(root_set):
            remaining_targets = [
                target_id
                for target_id in target_ids
                if target_id not in root_set and totals[target_id][0] < route_cap
            ]
            if not remaining_targets:
                break
            per_root = self._root_friend_circle_metrics(
                conn,
                root_id,
                reachable_ids,
                remaining_targets,
                depth,
                project_id,
                route_cap=route_cap,
            )
            for target_id in remaining_targets:
                remaining = route_cap - totals[target_id][0]
                count, hops, _ = per_root.get(target_id, (0, 0, 0.0))
                if count > remaining and count:
                    hops = round(hops * remaining / count)
                    count = remaining
                totals[target_id][0] += count
                totals[target_id][1] += hops

        metrics: dict[str, tuple[int, int, float]] = {}
        for target_id, (count, hops) in totals.items():
            if target_id in root_set:
                metrics[target_id] = (1, 0, 1_000_000.0)
            else:
                metrics[target_id] = (
                    count,
                    hops,
                    float(count * 1000 - hops) if count else 0.0,
                )
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
        display_filters: list[str] = []
        params: dict[str, Any] = {
            "project_id": project_id,
            "project_ids": self._visible_project_ids(project_id),
        }
        requested_depth: int | None = depth
        traversal_depth_reached: int | None = None
        root_found: bool | None = None
        depth_incomplete = False

        if query:
            params["query"] = query.lower()
            display_filters.append(
                "(toLower(coalesce(n.persona_name, '')) CONTAINS $query OR n.steam_id CONTAINS $query)"
            )
        if category:
            params["category"] = category
            display_filters.append("coalesce(membership.category, '') = $category")
        if friend_count_min is not None:
            params["friend_count_min"] = friend_count_min
            display_filters.append("coalesce(n.friend_count, -1) >= $friend_count_min")
        if friend_count_max is not None:
            params["friend_count_max"] = friend_count_max
            display_filters.append("coalesce(n.friend_count, -1) <= $friend_count_max")
        if prior_pool_min_links:
            params["prior_pool_min_links"] = prior_pool_min_links
            display_filters.append(
                "coalesce(membership.prior_pool_link_count, 0) >= $prior_pool_min_links"
            )

        display_where = " AND ".join(display_filters) if display_filters else "TRUE"
        sort_map = {
            "depth": "coalesce(membership.depth_min, 999)",
            "degree": "degree",
            "friend_count": "coalesce(n.friend_count, -1)",
            "prior_pool_links": "coalesce(membership.prior_pool_link_count, 0)",
            "closeness": "coalesce(membership.root_closeness_score, 0)",
        }
        order_expr = sort_map.get(sort_by, sort_map["depth"])
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

        conn = self._get_conn()
        root_metrics: dict[str, tuple[int, int, float]] = {}
        root_ids = (
            sorted(dict.fromkeys(part.strip() for part in root.split(",") if part.strip()))[:5]
            if root
            else []
        )
        intersection_ids: set[str] = set()
        if root_ids:
            reachable_sets: list[set[str]] = []
            root_found = False
            traversal_depth_reached = 0
            for root_id in root_ids:
                ids, found, reached, incomplete = self._reachable_ids_from_root(
                    conn, root_id, depth, project_id
                )
                if found:
                    root_found = True
                    reachable_sets.append(set(ids))
                    traversal_depth_reached = max(traversal_depth_reached, reached)
                    depth_incomplete = depth_incomplete or incomplete
            if not root_found or not reachable_sets:
                return GraphResponse(
                    nodes=[],
                    edges=[],
                    requested_depth=requested_depth,
                    traversal_depth_reached=traversal_depth_reached,
                    root_found=root_found,
                    depth_incomplete=depth_incomplete,
                )
            reachable_ids = sorted(set().union(*reachable_sets))
            intersection_ids = {
                steam_id
                for steam_id in reachable_ids
                if sum(steam_id in reachable for reachable in reachable_sets) > 1
            }
            params["reachable_ids"] = reachable_ids
            params["root_ids"] = root_ids
            effective_limit = max(limit, len(root_ids))
            node_query = f"""
            MATCH (n:SteamUser)-[membership:IN_PROJECT]->(p:Project)
            WHERE p.id = $project_id
              AND n.steam_id IN $reachable_ids
              AND (n.steam_id IN $root_ids OR ({display_where}))
            OPTIONAL MATCH (n)-[rel:STEAM_FRIEND]-() WHERE coalesce(rel.project_id, '') IN $project_ids
            WITH n, membership, count(DISTINCT rel) AS degree
            RETURN n, membership, degree
            ORDER BY CASE WHEN n.steam_id IN $root_ids THEN 0 ELSE 1 END,
                     {order_expr} {direction}, degree DESC, n.steam_id ASC
            LIMIT $limit
            """
            params["limit"] = effective_limit + 1
        else:
            effective_limit = limit
            params["limit"] = limit + 1
            node_query = f"""
            MATCH (n:SteamUser)-[membership:IN_PROJECT]->(p:Project)
            WHERE p.id = $project_id AND ({display_where})
            OPTIONAL MATCH (n)-[rel:STEAM_FRIEND]-()
            WHERE coalesce(rel.project_id, '') IN $project_ids
            WITH n, membership, count(DISTINCT rel) AS degree
            RETURN n, membership, degree
            ORDER BY {order_expr} {direction}, degree DESC
            LIMIT $limit
            """

        records = _consume_rows(conn.execute(node_query, params))
        limited = len(records) > effective_limit
        records = records[:effective_limit]
        nodes = [self._graph_node(_parse_node(rec[0]), rec[2], rec[1]) for rec in records]
        root_set = set(root_ids)
        for node in nodes:
            node.is_root = node.id in root_set
            node.is_intersection = node.id in intersection_ids
        if root_ids and nodes:
            root_metrics = self._multi_root_friend_circle_metrics(
                conn,
                root_ids,
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

        edge_rows = _consume_rows(
            conn.execute(
                """
                MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                WHERE a.steam_id IN $ids AND b.steam_id IN $ids AND a.steam_id < b.steam_id
                  AND coalesce(r.project_id, '') IN $project_ids
                RETURN a.steam_id, b.steam_id
                LIMIT 5000
                """,
                {"ids": ids, "project_ids": self._visible_project_ids(project_id)},
            )
        )

        edges = []
        for row in edge_rows:
            edges.append(
                GraphEdge(id=f"{row[0]}-{row[1]}", source=row[0], target=row[1], strength=1)
            )
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
        endpoint_metadata = self._project_metadata(conn, [from_id, to_id], project_id)
        if from_id not in endpoint_metadata or to_id not in endpoint_metadata:
            return GraphResponse(nodes=[], edges=[])

        _, depths, parents, root_found, _ = self._bfs_from_root(
            conn, from_id, max_depth, project_id
        )
        if not root_found or to_id not in depths:
            return GraphResponse(nodes=[], edges=[])

        path_ids = [to_id]
        while path_ids[-1] != from_id:
            parent = parents.get(path_ids[-1])
            if parent is None:
                return GraphResponse(nodes=[], edges=[])
            path_ids.append(parent)
        path_ids.reverse()

        users = self._users_by_id(conn, path_ids)
        metadata = self._project_metadata(conn, path_ids, project_id)
        nodes = []
        for steam_id in path_ids:
            node_dict = users.get(steam_id)
            if node_dict is None:
                return GraphResponse(nodes=[], edges=[])
            nodes.append(self._graph_node(node_dict, 0, metadata.get(steam_id)))

        edges = []
        for index in range(len(nodes) - 1):
            source = nodes[index].id
            target = nodes[index + 1].id
            edges.append(
                GraphEdge(id=f"{source}-{target}", source=source, target=target, strength=1)
            )
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
        ordered_ids, depths, _, root_found, _ = self._bfs_from_root(
            conn, root, max_depth, project_id
        )
        if not root_found:
            return FriendCircleAnalysisResponse(root=root, candidates=[])

        candidate_ids = [steam_id for steam_id in ordered_ids if depths[steam_id] >= 2]
        if not candidate_ids:
            return FriendCircleAnalysisResponse(root=root, candidates=[])

        edge_rows = _consume_rows(
            conn.execute(
                """
                MATCH (candidate:SteamUser)-[rel:STEAM_FRIEND]-(neighbor:SteamUser)
                WHERE candidate.steam_id IN $candidate_ids
                  AND coalesce(rel.project_id, '') IN $project_ids
                RETURN candidate.steam_id, neighbor.steam_id
                """,
                {
                    "candidate_ids": candidate_ids,
                    "project_ids": self._visible_project_ids(project_id),
                },
            )
        )
        neighbors = {steam_id: set() for steam_id in candidate_ids}
        for candidate_id, neighbor_id in edge_rows:
            if candidate_id != neighbor_id:
                neighbors.setdefault(candidate_id, set()).add(neighbor_id)

        candidate_users = self._users_by_id(conn, candidate_ids)
        candidate_metadata = self._project_metadata(conn, candidate_ids, project_id)
        ranked: list[tuple[float, int, str, list[str]]] = []
        for candidate_id in candidate_ids:
            if candidate_id not in candidate_users:
                continue
            candidate_depth = depths[candidate_id]
            evidence_ids = sorted(
                (
                    neighbor_id
                    for neighbor_id in neighbors.get(candidate_id, set())
                    if depths.get(neighbor_id, max_depth + 1) < candidate_depth
                ),
                key=lambda steam_id: (depths.get(steam_id, max_depth + 1), steam_id),
            )
            mutual_count = len(evidence_ids)
            if mutual_count < min_mutual:
                continue
            node_dict = candidate_users.get(candidate_id, {})
            degree = len(neighbors.get(candidate_id, set()))
            friend_count = node_dict.get("friend_count") or 0
            score = round(
                mutual_count * 10 + degree * 0.2 + friend_count / 100.0 - candidate_depth * 3,
                2,
            )
            ranked.append((score, mutual_count, candidate_id, evidence_ids[:6]))

        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        ranked = ranked[:limit]
        evidence_ids = list(dict.fromkeys(evidence_id for row in ranked for evidence_id in row[3]))
        evidence_users = self._users_by_id(conn, evidence_ids)
        evidence_metadata = self._project_metadata(conn, evidence_ids, project_id)

        candidates = []
        for score, mutual_count, candidate_id, candidate_evidence_ids in ranked:
            node = self._graph_node(
                candidate_users[candidate_id],
                len(neighbors.get(candidate_id, set())),
                candidate_metadata.get(candidate_id),
            )
            evidence = [
                self._graph_node(
                    evidence_users[evidence_id],
                    0,
                    evidence_metadata.get(evidence_id),
                )
                for evidence_id in candidate_evidence_ids
                if evidence_id in evidence_users
            ]
            candidates.append(
                FriendCircleCandidate(
                    steam_id=node.id,
                    label=node.label,
                    depth=depths[candidate_id],
                    avatar=node.avatar,
                    profile_url=node.profile_url,
                    degree=node.degree,
                    friend_count=node.friend_count,
                    mutual_count=mutual_count,
                    score=score,
                    evidence=evidence,
                )
            )
        return FriendCircleAnalysisResponse(root=root, candidates=candidates)

    def get_potential_friends(
        self,
        root: str,
        max_depth: int = 3,
        min_mutual: int = 2,
        limit: int = 50,
        project_id: str = "default",
    ) -> PotentialFriendsResponse:
        del max_depth  # The mutual-friend/Jaccard definition is inherently two-hop.
        min_mutual = max(0, min(min_mutual, 10000))
        limit = max(1, min(limit, 100))
        conn = self._get_conn()
        reachable_ids, depths, _, root_found, _ = self._bfs_from_root(conn, root, 2, project_id)
        if not root_found:
            return PotentialFriendsResponse(root=root, candidates=[])

        candidate_ids = sorted(steam_id for steam_id in reachable_ids if depths.get(steam_id) == 2)
        if not candidate_ids:
            return PotentialFriendsResponse(root=root, candidates=[])

        rows = _consume_rows(
            conn.execute(
                """
                MATCH (a:SteamUser)-[rel:STEAM_FRIEND]-(b:SteamUser)
                WHERE (a.steam_id = $root OR a.steam_id IN $candidate_ids)
                  AND coalesce(rel.project_id, '') IN $project_ids
                RETURN DISTINCT a.steam_id, b.steam_id
                """,
                {
                    "root": root,
                    "candidate_ids": candidate_ids,
                    "project_ids": self._visible_project_ids(project_id),
                },
            )
        )
        neighbors: dict[str, set[str]] = {root: set()}
        for candidate_id in candidate_ids:
            neighbors[candidate_id] = set()
        for source, target in rows:
            neighbors.setdefault(source, set()).add(target)

        users = self._users_by_id(conn, candidate_ids)
        metadata = self._project_metadata(conn, candidate_ids, project_id)
        root_neighbors = neighbors.get(root, set())
        ranked: list[PotentialFriendCandidate] = []
        for candidate_id in candidate_ids:
            if candidate_id not in users or candidate_id not in metadata:
                continue
            candidate_neighbors = neighbors.get(candidate_id, set())
            evidence_ids = sorted(root_neighbors & candidate_neighbors)
            mutual_count = len(evidence_ids)
            if mutual_count < min_mutual:
                continue
            union_count = len(root_neighbors | candidate_neighbors)
            jaccard = mutual_count / union_count if union_count else 0.0
            node = self._graph_node(
                users[candidate_id], len(candidate_neighbors), metadata[candidate_id]
            )
            evidence_users = self._users_by_id(conn, evidence_ids[:6])
            evidence_metadata = self._project_metadata(conn, evidence_ids[:6], project_id)
            ranked.append(
                PotentialFriendCandidate(
                    steam_id=node.id,
                    label=node.label,
                    depth=2,
                    avatar=node.avatar,
                    profile_url=node.profile_url,
                    degree=node.degree,
                    friend_count=node.friend_count,
                    mutual_count=mutual_count,
                    jaccard_coefficient=round(jaccard, 4),
                    score=round(jaccard * 100, 2),
                    evidence=[
                        self._graph_node(
                            evidence_users[evidence_id],
                            0,
                            evidence_metadata.get(evidence_id),
                        )
                        for evidence_id in evidence_ids[:6]
                        if evidence_id in evidence_users
                    ],
                )
            )
        ranked.sort(
            key=lambda candidate: (
                -candidate.score,
                -candidate.mutual_count,
                candidate.steam_id,
            )
        )
        return PotentialFriendsResponse(root=root, candidates=ranked[:limit])

    def get_top_degree(self, limit: int = 12, project_id: str = "default") -> list[GraphNode]:
        conn = self._get_conn()
        nodes = []
        for row in _iter_rows(
            conn.execute(
                """
                MATCH (n:SteamUser)-[membership:IN_PROJECT]->(p:Project)
                WHERE p.id = $project_id
                OPTIONAL MATCH (n)-[r:STEAM_FRIEND]-()
                WHERE coalesce(r.project_id, '') IN $project_ids
                WITH n, membership, count(DISTINCT r) AS degree
                RETURN n, membership, degree
                ORDER BY degree DESC
                LIMIT $limit
                """,
                {
                    "project_id": project_id,
                    "project_ids": self._visible_project_ids(project_id),
                    "limit": limit,
                },
            )
        ):
            node_dict = _parse_node(row[0])
            nodes.append(self._graph_node(node_dict, row[2], row[1]))
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

        latest_rows = _consume_rows(
            conn.execute(
                """
                MATCH (latest:CrawlRun)
                WHERE coalesce(latest.project_id, '') IN $project_ids
                RETURN latest
                ORDER BY latest.started_at DESC
                LIMIT 1
                """,
                {"project_ids": project_ids},
            )
        )
        latest = None
        if latest_rows:
            data = _parse_node(latest_rows[0][0])
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
        nodes = []
        for row in _iter_rows(
            conn.execute(
                """
                MATCH (n:SteamUser)-[membership:IN_PROJECT]->(p:Project)
                WHERE p.id = $project_id
                RETURN n, membership
                ORDER BY membership.depth_min, n.persona_name
                """,
                {"project_id": project_id},
            )
        ):
            node = _parse_node(row[0])
            membership = row[1] if isinstance(row[1], dict) else {}
            for key in _PROJECT_MEMBER_PROPERTY_TYPES:
                node[key] = membership.get(key)
            node["project_id"] = project_id
            nodes.append(node)

        edges = [
            {"source": row[0], "target": row[1]}
            for row in _iter_rows(
                conn.execute(
                    """
                    MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                    WHERE a.steam_id < b.steam_id AND coalesce(r.project_id, '') IN $project_ids
                    RETURN a.steam_id, b.steam_id
                    """,
                    {"project_ids": self._visible_project_ids(project_id)},
                )
            )
        ]

        return ExportResponse(nodes=nodes, edges=edges)
