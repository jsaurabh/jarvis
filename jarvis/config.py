from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    jarvis_model: str = "gpt-6-luna"
    jarvis_base_url: str | None = None
    jarvis_allowed_roots: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path.cwd()]
    )
    jarvis_enable_writes: bool = True
    jarvis_enable_commands: bool = True
    jarvis_enable_web_search: bool = True
    jarvis_trace_mode: str = "full"
    jarvis_phoenix_port: int = 6006
    jarvis_db_path: Path = Path("./data/jarvis.db")
    jarvis_tool_output_limit: int = 40_000
    jarvis_skills_path: Path = Path("./data/skills")

    @field_validator("jarvis_allowed_roots", mode="before")
    @classmethod
    def split_roots(cls, value: object) -> object:
        if isinstance(value, str):
            return [Path(item).expanduser() for item in value.split(os.pathsep) if item]
        return value

    @field_validator("jarvis_trace_mode")
    @classmethod
    def valid_trace_mode(cls, value: str) -> str:
        if value not in {"full", "metadata", "off"}:
            raise ValueError("JARVIS_TRACE_MODE must be full, metadata, or off")
        return value

    @field_validator("jarvis_phoenix_port")
    @classmethod
    def valid_phoenix_port(cls, value: int) -> int:
        if not 1024 <= value <= 65535:
            raise ValueError("JARVIS_PHOENIX_PORT must be between 1024 and 65535")
        return value

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return tuple(path.expanduser().resolve() for path in self.jarvis_allowed_roots)


settings = Settings()
