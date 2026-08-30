"""Application settings, loaded from environment / .env via pydantic-settings."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration. Every field maps 1:1 to an env var (see .env.example)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")
    anthropic_model: str | None = Field(default=None, alias="ANTHROPIC_MODEL")
    model: str = Field(default="claude-sonnet-4-5", alias="FOOTY_MODEL")
    headless: bool = Field(default=True, alias="FOOTY_HEADLESS")
    output_dir: Path = Field(default=Path("data"), alias="FOOTY_OUTPUT_DIR")
    timeout_ms: int = Field(default=30_000, alias="FOOTY_TIMEOUT_MS")
    snapshot_max_chars: int = Field(default=30_000, alias="FOOTY_SNAPSHOT_MAX_CHARS")

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key)

    def resolve_model(self, cli_model: str | None = None) -> str:
        """Model precedence: ``--model`` > ``ANTHROPIC_MODEL`` > ``FOOTY_MODEL``."""
        return cli_model or self.anthropic_model or self.model
