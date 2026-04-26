from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / "backend" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    s3_bucket: str = Field("", alias="S3_BUCKET")
    aws_region: str = Field(
        "ap-southeast-2",
        validation_alias=AliasChoices("APP_AWS_REGION", "AWS_REGION"),
    )
    aws_profile: str = Field("", alias="AWS_PROFILE")
    s3_base_prefix: str = Field("", alias="S3_BASE_PREFIX")
    max_file_bytes: int = Field(5 * 1024 * 1024, alias="MAX_FILE_BYTES")
    max_manifest_files: int = Field(5000, alias="MAX_MANIFEST_FILES")
    allowed_origins: str = Field("", alias="ALLOWED_ORIGINS")
    upload_session_store: str = Field("auto", alias="UPLOAD_SESSION_STORE")
    session_manifest_prefix: str = Field(".aws-browser/sessions", alias="SESSION_MANIFEST_PREFIX")
    app_api_token: str = Field("", alias="APP_API_TOKEN")

    @property
    def normalized_base_prefix(self) -> str:
        prefix = self.s3_base_prefix.strip("/")
        return f"{prefix}/" if prefix else ""

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
