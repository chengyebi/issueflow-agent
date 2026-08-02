from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    database_url: str = "postgresql://postgres:postgres@localhost:5432/issueflow"
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: SecretStr = SecretStr("")
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2022-11-28"

    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    chat_model: str | None = None
    agent_mode: Literal["workflow", "multi_agent"] = "workflow"
    prompt_version: str = "triage-v2"
    agent_version: str = "workflow-v2"

    rq_queue_name: str = "issueflow"
    agent_job_timeout_seconds: int = 180
    command_job_timeout_seconds: int = 120
    rq_max_retries: int = 2
    rq_retry_intervals: str = "10,30"
    outbox_max_attempts: int = 5
    outbox_base_backoff_seconds: int = 5
    llm_input_cost_per_million_usd: float | None = None
    llm_output_cost_per_million_usd: float | None = None

    @property
    def retry_intervals(self) -> list[int]:
        return [
            int(item.strip())
            for item in self.rq_retry_intervals.split(",")
            if item.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
