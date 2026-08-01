from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from neo4j import GraphDatabase

from .graph_repo import IGraphRepository
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


class Neo4jRepositoryImpl(IGraphRepository):
    """Neo4j 数据访问层。

    Security note: Cypher 查询中深度值经过 ``_safe_depth()`` 校验后以 f-string
    形式拼接。Neo4j 不支持参数化变长路径模式 ``*..$depth``，因此必须在应用层
    保证 depth 为安全整数后方可内插。
    """

    def __init__(self, uri: str, user: str, password: str) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    @staticmethod
    def _safe_depth(value: int, maximum: int = 4) -> int:
        """将深度值钳制到安全范围，供 f-string 拼接 Cypher 查询使用。"""
        return max(0, min(int(value), maximum))

    @staticmethod
    def _visible_project_ids(project_id: str) -> list[str]:
        return ["", project_id] if project_id == "default" else [project_id]

    def test_connection(self) -> str:
        self.driver.verify_connectivity()
        return "Neo4j 连接正常"

    def recover_interrupted_crawls(self) -> int:
        message = "应用重启前抓取未正常结束"
        with self.driver.session() as session:
            record = session.run(
                """
                MATCH (c:CrawlRun)
                WHERE c.status IN $statuses
                SET c.status = $status,
                    c.finished_at = $finished_at,
                    c.message = $message,
                    c.last_event = $message
                RETURN count(c) AS count
                """,
                statuses=[
                    CrawlStatus.pending.value,
                    CrawlStatus.running.value,
                    CrawlStatus.paused.value,
                ],
                status=CrawlStatus.stopped.value,
                finished_at=utc_now_iso(),
                message=message,
            ).single()
        return int(record["count"] or 0) if record else 0

    def ensure_schema(self) -> None:
        # 约束保证 MERGE 的唯一键稳定，也能让后续查询更快。
        statements = [
            "CREATE CONSTRAINT steam_user_id IF NOT EXISTS FOR (u:SteamUser) REQUIRE u.steam_id IS UNIQUE",
            "CREATE CONSTRAINT crawl_run_id IF NOT EXISTS FOR (r:CrawlRun) REQUIRE r.id IS UNIQUE",
            "CREATE CONSTRAINT project_id IF NOT EXISTS FOR (p:Project) REQUIRE p.id IS UNIQUE",
            "CREATE CONSTRAINT schema_migration_id IF NOT EXISTS FOR (m:SchemaMigration) REQUIRE m.id IS UNIQUE",
        ]
        with self.driver.session() as session:
            for statement in statements:
                session.run(statement).consume()
            self._migrate_project_memberships(session)
            self._migrate_project_member_metadata(session)

    @staticmethod
    def _migrate_project_memberships(session: Any) -> None:
        migration_id = "project-membership-v1"
        if (
            session.run(
                "MATCH (m:SchemaMigration {id: $id}) RETURN m.id AS id",
                id=migration_id,
            ).single()
            is not None
        ):
            return

        now = utc_now_iso()
        session.run(
            """
            MERGE (p:Project {id: 'default'})
            ON CREATE SET p.name = '默认项目', p.created_at = $now
            """,
            now=now,
        ).consume()
        session.run(
            """
            MATCH (u:SteamUser)
            WITH u, CASE
                WHEN coalesce(u.project_id, '') = '' THEN 'default'
                ELSE u.project_id
            END AS project_id
            MERGE (p:Project {id: project_id})
            ON CREATE SET p.name = project_id, p.created_at = $now
            MERGE (u)-[:IN_PROJECT]->(p)
            """,
            now=now,
        ).consume()
        session.run(
            """
            MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
            WHERE a.steam_id < b.steam_id
            WITH a, b, CASE
                WHEN coalesce(r.project_id, '') = '' THEN 'default'
                ELSE r.project_id
            END AS project_id
            MERGE (p:Project {id: project_id})
            ON CREATE SET p.name = project_id, p.created_at = $now
            MERGE (a)-[:IN_PROJECT]->(p)
            MERGE (b)-[:IN_PROJECT]->(p)
            """,
            now=now,
        ).consume()
        session.run(
            "MERGE (m:SchemaMigration {id: $id}) SET m.applied_at = $now",
            id=migration_id,
            now=now,
        ).consume()

    @staticmethod
    def _migrate_project_member_metadata(session: Any) -> None:
        migration_id = "project-member-metadata-v2"
        if (
            session.run(
                "MATCH (m:SchemaMigration {id: $id}) RETURN m.id AS id",
                id=migration_id,
            ).single()
            is not None
        ):
            return

        session.run(
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
                membership.tags = coalesce(u.tags, []),
                membership.category = coalesce(u.category, '')
            """
        ).consume()
        session.run(
            "MERGE (m:SchemaMigration {id: $id}) SET m.applied_at = $now",
            id=migration_id,
            now=utc_now_iso(),
        ).consume()

    # ── Project management ────────────────────────────────────────────

    def ensure_default_project(self) -> str:
        """Ensure the 'default' project exists and return its id."""
        return self.create_project(ProjectCreate(name="默认项目"), project_id="default")

    def create_project(
        self, payload: ProjectCreate, project_id: str | None = None
    ) -> str:
        import uuid

        pid = project_id or str(uuid.uuid4())
        now = utc_now_iso()
        with self.driver.session() as session:
            session.run(
                """
                MERGE (p:Project {id: $pid})
                ON CREATE SET p.name = $name, p.created_at = $now
                ON MATCH SET p.name = $name
                """,
                pid=pid,
                name=payload.name,
                now=now,
            ).consume()
        return pid

    def delete_project(self, project_id: str) -> bool:
        if project_id == "default":
            return False

        def delete_in_transaction(tx: Any) -> bool:
            result = tx.run(
                "MATCH (p:Project {id: $pid}) RETURN p",
                pid=project_id,
            ).single()
            if result is None:
                return False
            tx.run(
                """
                MATCH ()-[r:STEAM_FRIEND {project_id: $pid}]-()
                DELETE r
                """,
                pid=project_id,
            ).consume()
            tx.run(
                """
                MATCH (r:CrawlRun {project_id: $pid})
                DETACH DELETE r
                """,
                pid=project_id,
            ).consume()
            tx.run(
                """
                MATCH (p:Project {id: $pid})
                DETACH DELETE p
                """,
                pid=project_id,
            ).consume()
            tx.run(
                """
                MATCH (u:SteamUser)
                WHERE NOT EXISTS { MATCH (u)-[:IN_PROJECT]->(:Project) }
                  AND NOT EXISTS { MATCH (u)-[:STEAM_FRIEND]-(:SteamUser) }
                DETACH DELETE u
                """
            ).consume()
            return True

        with self.driver.session() as session:
            return session.execute_write(delete_in_transaction)

    def project_exists(self, project_id: str) -> bool:
        with self.driver.session() as session:
            result = session.run(
                "MATCH (p:Project {id: $pid}) RETURN p", pid=project_id
            ).single()
            return result is not None

    def list_projects(self) -> ProjectListResponse:
        with self.driver.session() as session:
            project_records = list(
                session.run(
                    """
                MATCH (p:Project)
                RETURN p.id AS id, p.name AS name, p.created_at AS created_at
                ORDER BY p.created_at DESC
                """
                )
            )
            if project_records:
                user_records = list(
                    session.run(
                        """
                    MATCH (:SteamUser)-[:IN_PROJECT]->(p:Project)
                    RETURN p.id AS project_id, count(*) AS user_count
                    """
                    )
                )
                relationship_records = list(
                    session.run(
                        """
                    MATCH ()-[r:STEAM_FRIEND]-()
                    WITH CASE
                        WHEN coalesce(r.project_id, '') = '' THEN 'default'
                        ELSE r.project_id
                    END AS project_id, r
                    RETURN project_id, count(DISTINCT r) AS relationship_count
                    """
                    )
                )
                crawl_records = list(
                    session.run(
                        """
                    MATCH (c:CrawlRun)
                    WITH CASE
                        WHEN coalesce(c.project_id, '') = '' THEN 'default'
                        ELSE c.project_id
                    END AS project_id
                    RETURN project_id, count(*) AS crawl_count
                    """
                    )
                )
            else:
                user_records = []
                relationship_records = []
                crawl_records = []
        if not project_records:
            # 首次启动：确保默认项目和 schema 存在
            self.ensure_schema()
            self.ensure_default_project()
            return ProjectListResponse(
                projects=[
                    ProjectInfo(id="default", name="默认项目", created_at=utc_now_iso())
                ],
                active_project_id="",
            )
        user_counts = {
            record["project_id"]: int(record["user_count"] or 0)
            for record in user_records
        }
        relationship_counts = {
            record["project_id"]: int(record["relationship_count"] or 0)
            for record in relationship_records
        }
        crawl_counts = {
            record["project_id"]: int(record["crawl_count"] or 0)
            for record in crawl_records
        }
        projects = [
            ProjectInfo(
                id=record["id"],
                name=record["name"] or "",
                created_at=record["created_at"] or "",
                steam_users=user_counts.get(record["id"], 0),
                relationships=relationship_counts.get(record["id"], 0),
                crawl_runs=crawl_counts.get(record["id"], 0),
            )
            for record in project_records
        ]
        return ProjectListResponse(projects=projects, active_project_id="")

    # ── Data operations (project-scoped) ──────────────────────────────

    def start_crawl_run(self, run: CrawlRun, project_id: str) -> None:
        data = run.model_dump(mode="json")
        data["project_id"] = project_id
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
                    r.prior_pool_filtered_count = $prior_pool_filtered_count,
                    r.project_id = $project_id
                """,
                **data,
            ).consume()

    def update_crawl_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"r.{key} = ${key}" for key in fields)
        with self.driver.session() as session:
            session.run(
                f"MATCH (r:CrawlRun {{id: $run_id}}) SET {assignments}",
                run_id=run_id,
                **fields,
            ).consume()

    def get_crawl_run(self, run_id: str) -> CrawlRun | None:
        with self.driver.session() as session:
            record = session.run(
                "MATCH (r:CrawlRun {id: $run_id}) RETURN r", run_id=run_id
            ).single()
        if record is None:
            return None
        return CrawlRun(**dict(record["r"]))

    def upsert_users(self, users: Iterable[SteamUserRecord], project_id: str) -> None:
        rows = [user.model_dump(mode="json") for user in users]
        if not rows:
            return
        now = utc_now_iso()
        batch_size = 1000
        with self.driver.session() as session:
            # Steam profile fields are global; project-specific crawl and annotation
            # fields live on the IN_PROJECT membership relationship.
            for i in range(0, len(rows), batch_size):
                batch_rows = rows[i : i + batch_size]
                session.run(
                    """
                    MERGE (p:Project {id: $project_id})
                    ON CREATE SET p.name = $project_name, p.created_at = $now
                    WITH p
                    UNWIND $users AS user
                    MERGE (u:SteamUser {steam_id: user.steam_id})
                    ON CREATE SET u.first_seen_at = $now
                    SET u.last_seen_at = $now,
                        u.project_id = CASE
                            WHEN u.project_id IS NULL OR u.project_id = '' THEN $project_id
                            ELSE u.project_id
                        END,
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
                        u.friend_list_status = CASE
                            WHEN user.friend_list_status = "unknown" THEN coalesce(u.friend_list_status, "unknown")
                            WHEN coalesce(u.friend_list_status, "unknown") = "private" THEN "private"
                            ELSE user.friend_list_status
                        END
                    MERGE (u)-[membership:IN_PROJECT]->(p)
                    SET membership.depth_min = CASE
                            WHEN membership.depth_min IS NULL OR user.depth_min < membership.depth_min THEN user.depth_min
                            ELSE membership.depth_min
                        END,
                        membership.prior_pool_link_count = CASE
                            WHEN user.prior_pool_link_count > coalesce(membership.prior_pool_link_count, 0) THEN user.prior_pool_link_count
                            ELSE coalesce(membership.prior_pool_link_count, 0)
                        END,
                        membership.root_closeness_score = CASE
                            WHEN user.root_closeness_score > coalesce(membership.root_closeness_score, 0) THEN user.root_closeness_score
                            ELSE coalesce(membership.root_closeness_score, 0)
                        END,
                        membership.last_scored_crawl_id = CASE
                            WHEN user.last_scored_crawl_id = "" THEN coalesce(membership.last_scored_crawl_id, "")
                            ELSE user.last_scored_crawl_id
                        END,
                        membership.note = coalesce(membership.note, ""),
                        membership.tags = coalesce(membership.tags, []),
                        membership.category = coalesce(membership.category, "")
                    """,
                    users=batch_rows,
                    now=now,
                    project_id=project_id,
                    project_name="默认项目" if project_id == "default" else project_id,
                ).consume()

    def mark_friend_list_status(
        self,
        steam_id: str,
        status: str,
        *,
        friend_count: int | None = None,
        friend_count_status: str | None = None,
        friend_ids: list[str] | None = None,
        project_id: str = "",
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
        batch_size = 1000
        with self.driver.session() as session:
            for offset in range(0, len(rows), batch_size):
                session.run(
                    """
                MERGE (p:Project {id: $project_id})
                ON CREATE SET p.name = $project_name, p.created_at = $now
                WITH p
                UNWIND $updates AS update
                MERGE (u:SteamUser {steam_id: update.steam_id})
                ON CREATE SET u.first_seen_at = $now
                SET u.friend_list_status = update.status,
                    u.project_id = CASE
                        WHEN u.project_id IS NULL OR u.project_id = '' THEN $project_id
                        ELSE u.project_id
                    END,
                    u.friend_count = CASE
                        WHEN update.friend_count IS NULL THEN u.friend_count
                        ELSE update.friend_count
                    END,
                    u.friend_count_status = CASE
                        WHEN update.friend_count_status IS NULL THEN coalesce(u.friend_count_status, "unknown")
                        ELSE update.friend_count_status
                    END,
                    u.friend_ids = CASE
                        WHEN update.friend_ids IS NULL THEN u.friend_ids
                        ELSE update.friend_ids
                    END,
                    u.friend_list_fetched_at = $now,
                    u.last_seen_at = $now
                MERGE (u)-[membership:IN_PROJECT]->(p)
                SET membership.note = coalesce(membership.note, ""),
                    membership.tags = coalesce(membership.tags, []),
                    membership.category = coalesce(membership.category, ""),
                    membership.prior_pool_link_count = coalesce(membership.prior_pool_link_count, 0),
                    membership.root_closeness_score = coalesce(membership.root_closeness_score, 0),
                    membership.last_scored_crawl_id = coalesce(membership.last_scored_crawl_id, "")
                    """,
                    updates=rows[offset : offset + batch_size],
                    project_id=project_id,
                    project_name="默认项目" if project_id == "default" else project_id,
                    now=now,
                ).consume()

    def get_cached_friend_list(
        self, steam_id: str, valid_days: int, project_id: str
    ) -> tuple[str, list[str]] | None:
        return self.get_cached_friend_lists([steam_id], valid_days, project_id).get(
            steam_id
        )

    def get_cached_friend_lists(
        self, steam_ids: Iterable[str], valid_days: int, project_id: str
    ) -> dict[str, tuple[str, list[str]]]:
        unique_ids = list(dict.fromkeys(steam_ids))
        if valid_days <= 0 or not unique_ids:
            return {}
        cutoff_time = (
            (datetime.now(UTC) - timedelta(days=valid_days))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.driver.session() as session:
            records = session.run(
                """
                MATCH (u:SteamUser)
                WHERE u.steam_id IN $steam_ids
                  AND u.friend_list_fetched_at >= $cutoff_time
                RETURN u.steam_id AS steam_id,
                       u.friend_list_status AS status,
                       u.friend_ids AS friend_ids
                """,
                steam_ids=unique_ids,
                cutoff_time=cutoff_time,
            )
            cached_lists: dict[str, tuple[str, list[str]]] = {}
            for record in records:
                steam_id = record["steam_id"]
                status = record["status"] or "unknown"
                if status == "unknown":
                    continue
                if status != "public":
                    cached_lists[steam_id] = (status, [])
                    continue
                friend_ids = record["friend_ids"]
                if friend_ids is None:
                    # Older rows did not persist the complete list; refetch them to self-heal.
                    continue
                cached_lists[steam_id] = (status, list(friend_ids))
            return cached_lists

    def upsert_relationships(
        self, edges: Iterable[FriendEdge], project_id: str
    ) -> None:
        rows = [edge.model_dump(mode="json") for edge in edges]
        if not rows:
            return
        now = utc_now_iso()
        batch_size = 1000
        with self.driver.session() as session:
            # Steam 好友关系按无向边处理，避免 A-B 和 B-A 重复出现。
            for i in range(0, len(rows), batch_size):
                batch_rows = rows[i : i + batch_size]
                session.run(
                    """
                    MERGE (p:Project {id: $project_id})
                    ON CREATE SET p.name = $project_name, p.created_at = $now
                    WITH p
                    UNWIND $edges AS edge
                    MATCH (a:SteamUser {steam_id: edge.from_id})
                    MATCH (b:SteamUser {steam_id: edge.to_id})
                    MERGE (a)-[a_membership:IN_PROJECT]->(p)
                    MERGE (b)-[b_membership:IN_PROJECT]->(p)
                    SET a_membership.note = coalesce(a_membership.note, ""),
                        a_membership.tags = coalesce(a_membership.tags, []),
                        a_membership.category = coalesce(a_membership.category, ""),
                        a_membership.prior_pool_link_count = coalesce(a_membership.prior_pool_link_count, 0),
                        a_membership.root_closeness_score = coalesce(a_membership.root_closeness_score, 0),
                        a_membership.last_scored_crawl_id = coalesce(a_membership.last_scored_crawl_id, ""),
                        b_membership.note = coalesce(b_membership.note, ""),
                        b_membership.tags = coalesce(b_membership.tags, []),
                        b_membership.category = coalesce(b_membership.category, ""),
                        b_membership.prior_pool_link_count = coalesce(b_membership.prior_pool_link_count, 0),
                        b_membership.root_closeness_score = coalesce(b_membership.root_closeness_score, 0),
                        b_membership.last_scored_crawl_id = coalesce(b_membership.last_scored_crawl_id, "")
                    MERGE (a)-[r:STEAM_FRIEND {project_id: $project_id}]-(b)
                    ON CREATE SET r.first_seen_at = $now
                    SET r.last_seen_at = $now,
                        r.crawl_id = edge.crawl_id,
                        r.source_depth = edge.source_depth
                    """,
                    edges=batch_rows,
                    now=now,
                    project_id=project_id,
                    project_name="默认项目" if project_id == "default" else project_id,
                ).consume()

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
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (u:SteamUser)-[membership:IN_PROJECT]->(p:Project)
                WHERE u.steam_id = $steam_id AND p.id = $project_id
                SET {assignments}
                """,
                steam_id=steam_id,
                project_id=project_id,
                **fields,
            ).consume()

    def bulk_patch_users(
        self, patches: Iterable[dict[str, Any]], project_id: str = "default"
    ) -> None:
        rows = []
        for p in patches:
            rows.append(
                {
                    "steam_id": p.get("steam_id"),
                    "note": p.get("note"),
                    "tags": p.get("tags"),
                    "category": p.get("category"),
                }
            )
        if not rows:
            return
        with self.driver.session() as session:
            session.run(
                """
                UNWIND $patches AS patch
                MATCH (u:SteamUser)-[membership:IN_PROJECT]->(p:Project)
                WHERE u.steam_id = patch.steam_id AND p.id = $project_id
                SET membership.note = CASE WHEN patch.note IS NOT NULL THEN patch.note ELSE membership.note END,
                    membership.tags = CASE WHEN patch.tags IS NOT NULL THEN patch.tags ELSE membership.tags END,
                    membership.category = CASE WHEN patch.category IS NOT NULL THEN patch.category ELSE membership.category END
                """,
                patches=rows,
                project_id=project_id,
            ).consume()

    def count_inner_layer_links(
        self, candidate_ids: list[str], inner_pool_ids: list[str], project_id: str
    ) -> dict[str, int]:
        """统计每个候选人与内层用户池（深度更浅的层）的连接数。"""
        if not candidate_ids or not inner_pool_ids:
            return {}
        with self.driver.session() as session:
            records = session.run(
                """
                UNWIND $candidates AS cid
                MATCH (c:SteamUser {steam_id: cid})-[r:STEAM_FRIEND]-(inner:SteamUser)
                WHERE inner.steam_id IN $inner_pool
                  AND coalesce(r.project_id, '') IN $project_ids
                RETURN cid AS candidate, count(DISTINCT inner) AS links
                """,
                candidates=candidate_ids,
                inner_pool=inner_pool_ids,
                project_ids=self._visible_project_ids(project_id),
            )
            return {row["candidate"]: row["links"] for row in records}

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
        depth = self._safe_depth(depth)
        limit = max(1, min(limit, 100000))
        filters = []
        params: dict[str, Any] = {
            "limit": limit,
            "project_id": project_id,
            "project_ids": self._visible_project_ids(project_id),
        }
        if query:
            params["query"] = query.lower()
            filters.append(
                "(toLower(coalesce(n.persona_name, '')) CONTAINS $query OR n.steam_id CONTAINS $query)"
            )
        if category:
            params["category"] = category
            filters.append("coalesce(membership.category, '') = $category")
        if friend_count_min is not None:
            params["friend_count_min"] = friend_count_min
            filters.append("coalesce(n.friend_count, -1) >= $friend_count_min")
        if friend_count_max is not None:
            params["friend_count_max"] = friend_count_max
            filters.append("coalesce(n.friend_count, -1) <= $friend_count_max")
        if prior_pool_min_links:
            params["prior_pool_min_links"] = prior_pool_min_links
            filters.append(
                "coalesce(membership.prior_pool_link_count, 0) >= $prior_pool_min_links"
            )
        where = "WHERE " + " AND ".join(filters) if filters else ""
        sort_map = {
            "depth": "coalesce(membership.depth_min, 999)",
            "degree": "degree",
            "friend_count": "coalesce(n.friend_count, -1)",
            "prior_pool_links": "coalesce(membership.prior_pool_link_count, 0)",
            "closeness": "coalesce(membership.root_closeness_score, 0)",
        }
        order_expr = sort_map.get(sort_by, sort_map["depth"])
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
        root_ids = (
            sorted(
                dict.fromkeys(part.strip() for part in root.split(",") if part.strip())
            )[:5]
            if root
            else []
        )
        with self.driver.session() as session:
            if root_ids:
                params["root_ids"] = root_ids
                # Root 查询只取指定层数内的子图，防止前端一次渲染过大的全库图。
                node_query = f"""
                MATCH (r:SteamUser)-[:IN_PROJECT]->(project:Project {{id: $project_id}})
                WHERE r.steam_id IN $root_ids
                MATCH p=(r)-[:STEAM_FRIEND*0..{depth}]-(n:SteamUser)
                WHERE all(rel IN relationships(p) WHERE coalesce(rel.project_id, '') IN $project_ids)
                WITH n, project, collect(DISTINCT r.steam_id) AS reached_from_roots
                MATCH (n)-[membership:IN_PROJECT]->(project)
                {where}
                RETURN n, membership, reached_from_roots, COUNT {{
                    (n)-[degree_rel:STEAM_FRIEND]-()
                    WHERE coalesce(degree_rel.project_id, '') IN $project_ids
                }} AS degree
                ORDER BY {order_expr} {direction}, degree DESC
                LIMIT $limit + 1
                """
            else:
                node_query = f"""
                MATCH (n:SteamUser)-[membership:IN_PROJECT]->(:Project {{id: $project_id}})
                {where}
                RETURN n, membership, COUNT {{
                    (n)-[degree_rel:STEAM_FRIEND]-()
                    WHERE coalesce(degree_rel.project_id, '') IN $project_ids
                }} AS degree
                ORDER BY {order_expr} {direction}, degree DESC
                LIMIT $limit + 1
                """
            records = list(session.run(node_query, **params))
            limited = len(records) > limit
            records = records[:limit]
            nodes = [
                self._graph_node(record["n"], record["degree"], record["membership"])
                for record in records
            ]
            if root_ids:
                for node, record in zip(nodes, records, strict=True):
                    node.is_intersection = len(record["reached_from_roots"]) > 1
            ids = [node.id for node in nodes]
            edge_records = list(
                session.run(
                    """
                    MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                    WHERE a.steam_id IN $ids AND b.steam_id IN $ids AND a.steam_id < b.steam_id
                      AND coalesce(r.project_id, '') IN $project_ids
                    RETURN a.steam_id AS source,
                           b.steam_id AS target,
                           COUNT {
                             MATCH (a)-[left_rel:STEAM_FRIEND]-(:SteamUser)-[right_rel:STEAM_FRIEND]-(b)
                             WHERE coalesce(left_rel.project_id, '') IN $project_ids
                               AND coalesce(right_rel.project_id, '') IN $project_ids
                           } AS strength
                    LIMIT 5000
                    """,
                    ids=ids,
                    project_ids=self._visible_project_ids(project_id),
                )
            )
        edges = [
            GraphEdge(
                id=f"{record['source']}-{record['target']}",
                source=record["source"],
                target=record["target"],
                strength=record["strength"] or 1,
            )
            for record in edge_records
        ]
        if root_ids and nodes:
            adjacency = {node.id: set() for node in nodes}
            for edge in edges:
                adjacency.setdefault(edge.source, set()).add(edge.target)
                adjacency.setdefault(edge.target, set()).add(edge.source)
            root_set = set(root_ids)
            for node in nodes:
                if node.id in root_set:
                    node.root_route_count = 1
                    node.root_route_total_hops = 0
                    node.root_friend_circle_score = 1_000_000.0
                    continue
                route_count = 0
                total_hops = 0

                def walk(current: str, remaining: int, visited: set[str]) -> None:
                    nonlocal route_count, total_hops
                    if remaining <= 0 or route_count >= 200:
                        return
                    for neighbor in sorted(adjacency.get(current, ())):
                        if route_count >= 200:
                            return
                        if neighbor in visited:
                            continue
                        if neighbor == node.id:
                            route_count += 1
                            total_hops += len(visited)
                            continue
                        visited.add(neighbor)
                        walk(neighbor, remaining - 1, visited)
                        visited.remove(neighbor)

                for root_id in sorted(root_set):
                    if root_id in adjacency and route_count < 200:
                        walk(root_id, depth, {root_id})
                node.root_route_count = route_count
                node.root_route_total_hops = total_hops
                node.root_friend_circle_score = (
                    float(route_count * 1000 - total_hops) if route_count else 0.0
                )
        return GraphResponse(nodes=nodes, edges=edges, limited=limited)

    def get_shortest_path(
        self, from_id: str, to_id: str, max_depth: int, project_id: str = "default"
    ) -> GraphResponse:
        max_depth = self._safe_depth(max_depth, 4)
        with self.driver.session() as session:
            record = session.run(
                f"""
                MATCH (a:SteamUser {{steam_id: $from_id}})-[:IN_PROJECT]->(project:Project {{id: $project_id}})
                MATCH (b:SteamUser {{steam_id: $to_id}})-[:IN_PROJECT]->(project)
                MATCH p=shortestPath((a:SteamUser {{steam_id: $from_id}})-[:STEAM_FRIEND*..{max_depth}]-(b:SteamUser {{steam_id: $to_id}}))
                WHERE all(r IN relationships(p) WHERE coalesce(r.project_id, '') IN $project_ids)
                RETURN nodes(p) AS nodes, relationships(p) AS rels
                """,
                from_id=from_id,
                to_id=to_id,
                project_id=project_id,
                project_ids=self._visible_project_ids(project_id),
            ).single()
            if record is None:
                return GraphResponse(nodes=[], edges=[])
            metadata = self._project_metadata(
                session,
                [node["steam_id"] for node in record["nodes"]],
                project_id,
            )
            nodes = [
                self._graph_node(node, 0, metadata.get(node["steam_id"]))
                for node in record["nodes"]
            ]
            edges = []
            path_nodes = record["nodes"]
            for index in range(len(path_nodes) - 1):
                source = path_nodes[index]["steam_id"]
                target = path_nodes[index + 1]["steam_id"]
                edges.append(
                    GraphEdge(
                        id=f"{source}-{target}",
                        source=source,
                        target=target,
                        strength=1,
                    )
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
        max_depth = self._safe_depth(max_depth, 4)
        min_mutual = max(0, min_mutual)
        limit = max(1, min(limit, 100))
        with self.driver.session() as session:
            records = list(
                session.run(
                    f"""
                    MATCH (root:SteamUser {{steam_id: $root}})-[:IN_PROJECT]->(project:Project {{id: $project_id}})
                    MATCH p=(root)-[:STEAM_FRIEND*2..{max_depth}]-(candidate:SteamUser)
                    WHERE all(r IN relationships(p) WHERE coalesce(r.project_id, '') IN $project_ids)
                    WITH root, candidate, min(length(p)) AS depth, project
                    MATCH (candidate)-[candidate_membership:IN_PROJECT]->(project)
                    WHERE candidate.steam_id <> $root
                      AND NOT EXISTS {{
                        MATCH (root)-[direct:STEAM_FRIEND]-(candidate)
                        WHERE coalesce(direct.project_id, '') IN $project_ids
                      }}
                    MATCH (candidate)-[evidence_rel:STEAM_FRIEND]-(evidence:SteamUser)
                    MATCH (evidence)-[evidence_membership:IN_PROJECT]->(project)
                    WHERE coalesce(evidence_rel.project_id, '') IN $project_ids
                      AND (
                        coalesce(evidence_membership.depth_min, 999) < coalesce(candidate_membership.depth_min, 999)
                        OR EXISTS {{
                          MATCH (root)-[root_rel:STEAM_FRIEND]-(evidence)
                          WHERE coalesce(root_rel.project_id, '') IN $project_ids
                        }}
                      )
                    WITH candidate,
                         candidate_membership,
                         depth,
                         collect(DISTINCT evidence)[0..6] AS evidence_nodes,
                         count(DISTINCT evidence) AS mutual_count,
                         COUNT {{
                           (candidate)-[degree_rel:STEAM_FRIEND]-()
                           WHERE coalesce(degree_rel.project_id, '') IN $project_ids
                         }} AS degree
                    WHERE mutual_count >= $min_mutual
                    RETURN candidate,
                           candidate_membership,
                           depth,
                           evidence_nodes,
                           mutual_count,
                           degree,
                           (mutual_count * 10 + degree * 0.2 + coalesce(candidate.friend_count, 0) / 100.0 - depth * 3) AS score
                    ORDER BY score DESC, mutual_count DESC
                    LIMIT $limit
                    """,
                    root=root,
                    project_id=project_id,
                    min_mutual=min_mutual,
                    limit=limit,
                    project_ids=self._visible_project_ids(project_id),
                )
            )
            evidence_metadata = self._project_metadata(
                session,
                [
                    evidence["steam_id"]
                    for record in records
                    for evidence in record["evidence_nodes"]
                ],
                project_id,
            )
        candidates = []
        for record in records:
            node = self._graph_node(
                record["candidate"],
                record["degree"],
                record["candidate_membership"],
            )
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
                    evidence=[
                        self._graph_node(
                            evidence,
                            0,
                            evidence_metadata.get(evidence["steam_id"]),
                        )
                        for evidence in record["evidence_nodes"]
                    ],
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
        project_ids = self._visible_project_ids(project_id)
        with self.driver.session() as session:
            records = list(
                session.run(
                    """
                    MATCH (root:SteamUser)-[:IN_PROJECT]->(project:Project {id: $project_id})
                    WHERE root.steam_id = $root
                    MATCH (root)-[left_rel:STEAM_FRIEND]-(mutual:SteamUser)
                          -[right_rel:STEAM_FRIEND]-(candidate:SteamUser)
                    MATCH (mutual)-[:IN_PROJECT]->(project)
                    MATCH (candidate)-[candidate_membership:IN_PROJECT]->(project)
                    WHERE candidate.steam_id <> root.steam_id
                      AND coalesce(left_rel.project_id, '') IN $project_ids
                      AND coalesce(right_rel.project_id, '') IN $project_ids
                      AND NOT EXISTS {
                        MATCH (root)-[direct_rel:STEAM_FRIEND]-(candidate)
                        WHERE coalesce(direct_rel.project_id, '') IN $project_ids
                      }
                    WITH root,
                         candidate,
                         candidate_membership,
                         collect(DISTINCT mutual)[0..6] AS evidence_nodes,
                         count(DISTINCT mutual) AS mutual_count,
                         COUNT {
                           (root)-[root_degree_rel:STEAM_FRIEND]-()
                           WHERE coalesce(root_degree_rel.project_id, '') IN $project_ids
                         } AS root_degree,
                         COUNT {
                           (candidate)-[candidate_degree_rel:STEAM_FRIEND]-()
                           WHERE coalesce(candidate_degree_rel.project_id, '') IN $project_ids
                         } AS candidate_degree
                    WHERE mutual_count >= $min_mutual
                    RETURN candidate,
                           candidate_membership,
                           evidence_nodes,
                           mutual_count,
                           root_degree,
                           candidate_degree
                    ORDER BY mutual_count DESC, candidate.steam_id
                    LIMIT 1000
                    """,
                    root=root,
                    project_id=project_id,
                    project_ids=project_ids,
                    min_mutual=min_mutual,
                )
            )
            evidence_metadata = self._project_metadata(
                session,
                [
                    evidence["steam_id"]
                    for record in records
                    for evidence in record["evidence_nodes"]
                ],
                project_id,
            )

        candidates: list[PotentialFriendCandidate] = []
        for record in records:
            mutual_count = int(record["mutual_count"] or 0)
            union_count = (
                int(record["root_degree"] or 0)
                + int(record["candidate_degree"] or 0)
                - mutual_count
            )
            jaccard = mutual_count / union_count if union_count else 0.0
            node = self._graph_node(
                record["candidate"],
                record["candidate_degree"],
                record["candidate_membership"],
            )
            candidates.append(
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
                            evidence,
                            0,
                            evidence_metadata.get(evidence["steam_id"]),
                        )
                        for evidence in record["evidence_nodes"]
                    ],
                )
            )
        candidates.sort(
            key=lambda candidate: (
                -candidate.score,
                -candidate.mutual_count,
                candidate.steam_id,
            )
        )
        return PotentialFriendsResponse(root=root, candidates=candidates[:limit])

    def get_top_degree(
        self, limit: int = 12, project_id: str = "default"
    ) -> list[GraphNode]:
        with self.driver.session() as session:
            records = list(
                session.run(
                    """
                    MATCH (n:SteamUser)-[membership:IN_PROJECT]->(:Project {id: $project_id})
                    RETURN n, membership, COUNT {
                        (n)-[degree_rel:STEAM_FRIEND]-()
                        WHERE coalesce(degree_rel.project_id, '') IN $project_ids
                    } AS degree
                    ORDER BY degree DESC
                    LIMIT $limit
                    """,
                    limit=max(1, min(limit, 50)),
                    project_id=project_id,
                    project_ids=self._visible_project_ids(project_id),
                )
            )
        return [
            self._graph_node(record["n"], record["degree"], record["membership"])
            for record in records
        ]

    def get_db_stats(self, project_id: str = "default") -> DbStats:
        with self.driver.session() as session:
            steam_users = session.run(
                """
                MATCH (u:SteamUser)-[:IN_PROJECT]->(:Project {id: $pid})
                RETURN count(u) AS count
                """,
                pid=project_id,
            ).single()["count"]
            relationships = session.run(
                "MATCH ()-[r:STEAM_FRIEND]->() WHERE coalesce(r.project_id, '') IN $project_ids RETURN count(r) AS count",
                project_ids=self._visible_project_ids(project_id),
            ).single()["count"]
            crawl_runs = session.run(
                "MATCH (c:CrawlRun) WHERE coalesce(c.project_id, '') IN $project_ids RETURN count(c) AS count",
                project_ids=self._visible_project_ids(project_id),
            ).single()["count"]
            latest_record = session.run(
                """
                MATCH (latest:CrawlRun)
                WHERE coalesce(latest.project_id, '') IN $project_ids
                RETURN latest
                ORDER BY latest.started_at DESC
                LIMIT 1
                """,
                project_ids=self._visible_project_ids(project_id),
            ).single()
        latest = latest_record["latest"] if latest_record is not None else None
        return DbStats(
            steam_users=steam_users or 0,
            steam_friend_relationships=relationships or 0,
            crawl_runs=crawl_runs or 0,
            latest_crawl=CrawlRun(**dict(latest)) if latest is not None else None,
        )

    def export_graph(self, project_id: str = "default") -> ExportResponse:
        with self.driver.session() as session:
            nodes = []
            for record in session.run(
                """
                MATCH (n:SteamUser)-[membership:IN_PROJECT]->(:Project {id: $pid})
                RETURN n, membership
                ORDER BY membership.depth_min, n.persona_name
                """,
                pid=project_id,
            ):
                node = dict(record["n"])
                node.update(dict(record["membership"]))
                node["project_id"] = project_id
                nodes.append(node)
            edges = [
                {"source": record["source"], "target": record["target"]}
                for record in session.run(
                    """
                    MATCH (a:SteamUser)-[r:STEAM_FRIEND]-(b:SteamUser)
                    WHERE a.steam_id < b.steam_id AND coalesce(r.project_id, '') IN $project_ids
                    RETURN a.steam_id AS source, b.steam_id AS target
                    ORDER BY source, target
                    """,
                    project_ids=self._visible_project_ids(project_id),
                )
            ]
        return ExportResponse(nodes=nodes, edges=edges)

    @staticmethod
    def _project_metadata(
        session: Any, steam_ids: list[str], project_id: str
    ) -> dict[str, dict[str, Any]]:
        if not steam_ids:
            return {}
        return {
            record["steam_id"]: dict(record["membership"])
            for record in session.run(
                """
                MATCH (u:SteamUser)-[membership:IN_PROJECT]->(:Project {id: $project_id})
                WHERE u.steam_id IN $steam_ids
                RETURN u.steam_id AS steam_id, membership
                """,
                steam_ids=steam_ids,
                project_id=project_id,
            )
        }

    @staticmethod
    def _graph_node(node: Any, degree: int, metadata: Any | None = None) -> GraphNode:
        data = dict(node)
        member = dict(metadata) if metadata is not None else {}
        return GraphNode(
            id=data.get("steam_id", ""),
            label=data.get("persona_name") or data.get("steam_id", "Unknown"),
            depth=member.get("depth_min"),
            avatar=data.get("avatar_full")
            or data.get("avatar_medium")
            or data.get("avatar")
            or "",
            profile_url=data.get("profile_url") or "",
            note=member.get("note") or "",
            tags=member.get("tags") or [],
            category=member.get("category") or "",
            friend_list_status=data.get("friend_list_status") or "unknown",
            degree=degree,
            friend_count=data.get("friend_count"),
            friend_count_status=data.get("friend_count_status") or "unknown",
            prior_pool_link_count=member.get("prior_pool_link_count") or 0,
            root_closeness_score=member.get("root_closeness_score") or 0,
        )
