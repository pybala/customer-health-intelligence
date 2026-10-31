from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration from environment variables. Missing or unknown values stop startup."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: str
    migration_database_url: str | None = None
    api_key_pepper: SecretStr
    auth_mode: Literal["api_keys", "dev_tokens"] = "api_keys"
    dev_tokens_file: Path = Path("config/dev_tokens.json")
    worker_org_ids: Annotated[list[UUID], NoDecode] = []
    outreach_mode: Literal["dry_run", "mock_send"] = "dry_run"
    email_adapter: Literal["mock"] = "mock"
    artifact_root: Path = Path("artifacts/models")
    # Illustrative CSM review share, not a validated precision or capacity claim.
    review_capacity_fraction: float = Field(default=0.10, gt=0, le=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("worker_org_ids", mode="before")
    @classmethod
    def _split_org_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("worker_org_ids")
    @classmethod
    def _unique_org_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("WORKER_ORG_IDS contains duplicates")
        return value

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("postgresql+psycopg://"):
            raise ValueError("database URLs must use the postgresql+psycopg:// driver")
        return value

    @field_validator("api_key_pepper")
    @classmethod
    def _pepper_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("API_KEY_PEPPER must be at least 32 characters")
        return value
