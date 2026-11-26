"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DATABASE_PATH = (PROJECT_ROOT / "database.db").resolve()


@dataclass(frozen=True)
class Settings:
    database_path: str
    max_upload_bytes: int
    cors_origins: tuple[str, ...]
    nvidia_api_key: str | None
    nvidia_api_base_url: str
    nvidia_model: str


settings = Settings(
    database_path=str(CANONICAL_DATABASE_PATH),
    max_upload_bytes=int(
        os.getenv("QUERYGLASS_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024 * 1024))
    ),
    cors_origins=tuple(
        origin.strip()
        for origin in os.getenv(
            "QUERYGLASS_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,"
            "http://localhost:3001,http://127.0.0.1:3001,"
            "http://localhost:3002,http://127.0.0.1:3002,"
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ),
    nvidia_api_key=os.getenv("NVIDIA_API_KEY"),
    nvidia_api_base_url=os.getenv(
        "NVIDIA_API_BASE_URL", "https://integrate.api.nvidia.com/v1"
    ),
    nvidia_model=os.getenv("NVIDIA_MODEL", "mistralai/mistral-large-4-1t-a52b"),
)
