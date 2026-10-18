import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/app.sqlite"
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    max_upload_size_mb: int = 50
    log_level: str = "info"
    app_password: str | None = Field(default=None)
    data_root: Path = Path("data")
    enable_scan_debug: bool = False

    # Demo mode (shared public demo only). Demo runs use a separate data root
    # (default ``data-demo/``) so the synthetic demo dataset never mixes with a
    # school's real local data. See docs/demo-mode.md.
    demo_mode: bool = False
    demo_reset_on_start: bool = False
    demo_data_root: Path = Path("data-demo")

    @model_validator(mode="after")
    def _apply_demo_data_defaults(self) -> "Settings":
        """Point a demo run at the separate demo data root by default.

        Only applied when the caller did not explicitly set ``DATABASE_URL`` or
        ``DATA_ROOT`` — explicit overrides (portable installs, hosted staging)
        always win.
        """
        # ``model_fields_set`` only covers init kwargs: an env/.env-sourced
        # DATABASE_URL/DATA_ROOT (portable install) would look "unset", so the
        # ambient environment is checked as well. Explicit overrides always win.
        env_overrides = {"DATABASE_URL", "DATA_ROOT"} & os.environ.keys()
        if not self.demo_mode or env_overrides:
            return self
        if "database_url" not in self.model_fields_set:
            self.database_url = "sqlite:///" + (self.demo_data_root / "app.sqlite").as_posix()
        if "data_root" not in self.model_fields_set:
            self.data_root = self.demo_data_root
        return self

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
