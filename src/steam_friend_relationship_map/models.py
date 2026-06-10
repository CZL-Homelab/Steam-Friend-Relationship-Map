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
