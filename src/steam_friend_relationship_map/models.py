from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CrawlStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class SteamUserRecord(BaseModel):
    steam_id: str
    persona_name: str = "Unknown"
    profile_url: str = ""
    avatar: str = ""
    avatar_medium: str = ""
    avatar_full: str = ""
    visibility_state: int | None = None
    profile_state: int | None = None
    depth_min: int = 0
    friend_list_status: str = "unknown"


class FriendEdge(BaseModel):
    from_id: str
    to_id: str
    crawl_id: str
    source_depth: int


class CrawlCreate(BaseModel):
    root_url: str = Field(min_length=1)
    max_depth: int = Field(default=2, ge=1, le=4)
    max_nodes: int = Field(default=2000, ge=1, le=10000)
    delay_ms: int = Field(default=300, ge=0, le=10000)


class CrawlRun(BaseModel):
    id: str
    root_steam_id: str
    max_depth: int
    max_nodes: int
    status: CrawlStatus
    started_at: str | None = None
    finished_at: str | None = None
    nodes_discovered: int = 0
    edges_discovered: int = 0
    private_count: int = 0
    error_count: int = 0
    message: str = ""
    current_depth: int | None = None
    current_steam_id: str = ""
    queue_size: int = 0
    expanded_count: int = 0
    progress_percent: int = 0
    last_event: str = ""


class CrawlEvent(BaseModel):
    seq: int
    run_id: str
    time: str
    level: str
    stage: str
    message: str


class UserPatch(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    category: str | None = Field(default=None, max_length=120)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return None
        cleaned = []
        for tag in tags:
            value = tag.strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned[:30]


class SettingsTestResult(BaseModel):
    steam_ok: bool
    neo4j_ok: bool
    steam_message: str
    neo4j_message: str


class PublicSettings(BaseModel):
    neo4j_uri: str
    neo4j_user: str
    app_host: str
    app_port: int
    default_max_depth: int
    default_max_nodes: int
    default_delay_ms: int
    steam_api_key_configured: bool
    neo4j_password_configured: bool
    steam_api_key_from_env: bool = False
    neo4j_password_from_env: bool = False
    secure_store_available: bool = True
    message: str = ""


class SettingsPatch(BaseModel):
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    app_host: str | None = None
    app_port: int | None = Field(default=None, ge=1, le=65535)
    default_max_depth: int | None = Field(default=None, ge=1, le=4)
    default_max_nodes: int | None = Field(default=None, ge=1, le=10000)
    default_delay_ms: int | None = Field(default=None, ge=0, le=10000)


class SecretUpdate(BaseModel):
    name: str
    value: str = Field(min_length=1)


class DbStats(BaseModel):
    steam_users: int = 0
    steam_friend_relationships: int = 0
    crawl_runs: int = 0
    latest_crawl: CrawlRun | None = None


class GraphNode(BaseModel):
    id: str
    label: str
    depth: int | None = None
    avatar: str = ""
    profile_url: str = ""
    note: str = ""
    tags: list[str] = []
    category: str = ""
    friend_list_status: str = "unknown"
    degree: int = 0


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    limited: bool = False


class ExportResponse(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
