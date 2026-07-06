from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from .models import (
    CrawlRun,
    DbStats,
    ExportResponse,
    FriendCircleAnalysisResponse,
    FriendEdge,
    GraphNode,
    GraphResponse,
    PotentialFriendCandidate,
    PotentialFriendsResponse,
    ProjectCreate,
    ProjectListResponse,
    SteamUserRecord,
)


class IGraphRepository(ABC):
    """图数据库抽象中间层接口。"""

    @abstractmethod
    def close(self) -> None:
        """关闭数据库连接与驱动资源。"""
        pass

    @abstractmethod
    def test_connection(self) -> str:
        """测试数据库连接状态并返回状态消息。"""
        pass

    @abstractmethod
    def ensure_schema(self) -> None:
        """初始化图数据库的约束、Schema和必要索引。"""
        pass

    @abstractmethod
    def list_projects(self) -> ProjectListResponse:
        """获取所有项目信息列表。"""
        pass

    @abstractmethod
    def create_project(self, payload: ProjectCreate, project_id: str | None = None) -> str:
        """创建新项目，返回项目 ID。"""
        pass

    @abstractmethod
    def delete_project(self, project_id: str) -> bool:
        """删除指定项目及其包含的所有节点和关系。"""
        pass

    @abstractmethod
    def project_exists(self, project_id: str) -> bool:
        """判断指定 ID 的项目是否存在。"""
        pass

    @abstractmethod
    def get_crawl_run(self, run_id: str) -> CrawlRun | None:
        """获取抓取任务详情。"""
        pass

    @abstractmethod
    def start_crawl_run(self, run: CrawlRun, project_id: str) -> None:
        """初始化并开始记录一个新的抓取任务状态。"""
        pass

    @abstractmethod
    def update_crawl_run(self, run_id: str, **fields: Any) -> None:
        """更新抓取任务运行时的状态与进度字段。"""
        pass

    @abstractmethod
    def upsert_users(self, users: Iterable[SteamUserRecord], project_id: str) -> None:
        """批量保存或更新用户节点。"""
        pass

    @abstractmethod
    def mark_friend_list_status(
        self,
        steam_id: str,
        status: str,
        friend_count: int | None,
        friend_count_status: str,
        friend_ids: list[str],
        project_id: str,
    ) -> None:
        """更新用户的关系抓取状态及好友关联，并建立好友关系边。"""
        pass

    @abstractmethod
    def get_cached_friend_list(
        self, steam_id: str, valid_days: int, project_id: str
    ) -> tuple[str, list[str]] | None:
        """获取处于有效期内的本地好友列表缓存，若失效或不存在则返回 None。"""
        pass

    @abstractmethod
    def upsert_relationships(self, edges: Iterable[FriendEdge], project_id: str) -> None:
        """批量更新或保存好友关系边。"""
        pass

    @abstractmethod
    def patch_user(
        self,
        steam_id: str,
        *,
        note: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
    ) -> None:
        """局部更新用户节点属性（备注、标签、分类）。"""
        pass

    @abstractmethod
    def bulk_patch_users(self, patches: Iterable[dict[str, Any]]) -> None:
        """批量局部更新用户节点属性（备注、标签、分类）。"""
        pass

    @abstractmethod
    def count_inner_layer_links(
        self, candidate_ids: list[str], inner_pool_ids: list[str], project_id: str
    ) -> dict[str, int]:
        """统计各个候选人与内层（已爬取）用户池的连接关系数量。"""
        pass

    @abstractmethod
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
        """根据查询过滤参数，查询子图节点和边用于渲染。"""
        pass

    @abstractmethod
    def get_shortest_path(
        self, from_id: str, to_id: str, max_depth: int, project_id: str = "default"
    ) -> GraphResponse:
        """获取两个 Steam 用户之间的最短社交关系路径。"""
        pass

    @abstractmethod
    def get_friend_circle_analysis(
        self,
        root: str,
        max_depth: int = 3,
        min_mutual: int = 2,
        limit: int = 50,
        project_id: str = "default",
    ) -> FriendCircleAnalysisResponse:
        """分析并获取用户的“朋友圈推荐”候选人列表（二度/三度潜在人脉社区）。"""
        pass

    @abstractmethod
    def get_potential_friends(
        self,
        root: str,
        max_depth: int = 3,
        min_mutual: int = 2,
        limit: int = 50,
        project_id: str = "default",
    ) -> PotentialFriendsResponse:
        """分析并获取二度/三度潜在好友推荐列表（基于 Jaccard 相似度）。"""
        pass

    @abstractmethod
    def get_top_degree(self, limit: int = 12, project_id: str = "default") -> list[GraphNode]:
        """获取当前项目中连接数最多的 Top 节点列表。"""
        pass

    @abstractmethod
    def get_db_stats(self, project_id: str = "default") -> DbStats:
        """获取当前项目的数据统计（节点数、关系数、私密用户数等）。"""
        pass

    @abstractmethod
    def export_graph(self, project_id: str = "default") -> ExportResponse:
        """导出当前项目的所有用户节点和好友关系边，用于文件备份。"""
        pass
