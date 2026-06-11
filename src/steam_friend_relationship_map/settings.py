from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .secrets import SecretStorageError, SecretStore


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    steam_api_key: str = Field(default="", alias="STEAM_API_KEY")
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="", alias="NEO4J_PASSWORD")
    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    default_max_depth: int = Field(default=2, ge=1, le=4, alias="DEFAULT_MAX_DEPTH")
    default_max_nodes: int = Field(default=2000, ge=1, le=10000, alias="DEFAULT_MAX_NODES")
    default_delay_ms: int = Field(default=300, ge=0, le=10000, alias="DEFAULT_DELAY_MS")
    default_cache_valid_days: int = Field(default=14, ge=0, alias="DEFAULT_CACHE_VALID_DAYS")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    store = SecretStore()
    updates = {}
    try:
        steam_api_key = store.get("steam_api_key")
        neo4j_password = store.get("neo4j_password")
    except SecretStorageError:
        steam_api_key = ""
        neo4j_password = ""
    if steam_api_key:
        updates["steam_api_key"] = steam_api_key
    if neo4j_password:
        updates["neo4j_password"] = neo4j_password
    return settings.model_copy(update=updates)


def clear_settings_cache() -> None:
    get_settings.cache_clear()
