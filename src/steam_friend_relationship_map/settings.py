from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .proxy import normalize_proxy_url
from .secrets import SecretStorageError, SecretStore


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    steam_api_key: str = Field(default="", alias="STEAM_API_KEY")
    steam_proxy_url: str = Field(default="", alias="STEAM_PROXY_URL")
    graph_db_engine: Literal["kuzu", "neo4j"] = Field(
        default="kuzu", alias="GRAPH_DB_ENGINE"
    )
    kuzu_db_path: str = Field(
        default="./data/graph_kuzu", min_length=1, alias="KUZU_DB_PATH"
    )
    kuzu_buffer_pool_size_gb: int = Field(
        default=1, ge=1, le=64, alias="KUZU_BUFFER_POOL_SIZE_GB"
    )
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="", alias="NEO4J_PASSWORD")
    app_host: str = Field(default="127.0.0.1", min_length=1, alias="APP_HOST")
    app_port: int = Field(default=8000, ge=1, le=65535, alias="APP_PORT")
    default_max_depth: int = Field(default=2, ge=1, le=4, alias="DEFAULT_MAX_DEPTH")
    default_max_nodes: int = Field(
        default=2000, ge=1, le=10000, alias="DEFAULT_MAX_NODES"
    )
    default_delay_ms: int = Field(default=300, ge=0, le=10000, alias="DEFAULT_DELAY_MS")
    default_cache_valid_days: int = Field(
        default=14, ge=0, alias="DEFAULT_CACHE_VALID_DAYS"
    )
    active_project: str = Field(default="default", min_length=1, alias="ACTIVE_PROJECT")

    @field_validator("steam_proxy_url")
    @classmethod
    def validate_steam_proxy_url(cls, value: str) -> str:
        return normalize_proxy_url(value)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    store = SecretStore()
    updates = {}
    for name in ("steam_api_key", "steam_proxy_url", "neo4j_password"):
        try:
            value = store.get(name)
        except SecretStorageError:
            continue
        if value:
            updates[name] = value
    return Settings.model_validate({**settings.model_dump(), **updates})


def clear_settings_cache() -> None:
    get_settings.cache_clear()
