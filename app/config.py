import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    telegram_bot_token: str = Field(default="", validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "tg"))
    telegram_bot_username: str = ""
    database_url: str = "sqlite:///./data/ebay.db"
    state_dir: Path = Path("data")
    public_url: str = "http://localhost:8000"
    cookie_secure: bool = False
    secret_key: str = ""
    access_minutes: int = 10
    refresh_days: int = 30
    request_delay_seconds: float = Field(default=5, ge=0)
    max_search_pages: int = Field(default=3, ge=1, le=10)
    details_per_scan: int = Field(default=12, ge=1, le=100)
    ebay_proxy: str = ""
    ebay_http_client: Literal["playwright", "curl_cffi", "httpx"] = "playwright"
    ebay_browser: str = Field(default="chrome", min_length=1, max_length=64)
    ebay_block_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    ebay_browser_profile_dir: Path | None = None
    ebay_browser_headless: bool = False
    ebay_browser_timeout_seconds: int = Field(default=45, ge=5, le=180)

    def signing_key(self) -> str:
        if self.secret_key:
            if len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must have at least 32 characters")
            return self.secret_key
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "secret.key"
        # Atomic creation: API, worker and bot must share the same signing key.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text().strip()
        with os.fdopen(fd, "w") as handle:
            key = secrets.token_urlsafe(64)
            handle.write(key)
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()

