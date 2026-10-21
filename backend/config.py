from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5432/health_agent",
    )
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "*").split(",")
        if origin.strip()
    )
    auto_create_tables: bool = os.getenv("AUTO_CREATE_TABLES", "false").lower() == "true"
    environment: str = os.getenv("APP_ENV", "development")


settings = Settings()
