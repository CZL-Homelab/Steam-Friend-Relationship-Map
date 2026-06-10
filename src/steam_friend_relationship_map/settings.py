from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
