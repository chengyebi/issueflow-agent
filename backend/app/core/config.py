from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
        populate_by_name=True,
    )

    database_url: str = "postgresql://postgres:postgres@localhost:5432/issueflow"
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: SecretStr = SecretStr("")
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2022-11-28"
    github_write_enabled: bool = True
    review_admin_token: SecretStr | None = None

    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    chat_model: str | None = None
    agent_mode: Literal["workflow", "multi_agent"] = "workflow"
    prompt_version: str = "triage-v2"
    agent_version: str = "workflow-v2"

    # 选择性自动化 rollout mode：off | shadow | enforce
    automation_mode: Literal["off", "shadow", "enforce"] = "shadow"
    # 冻结策略 artifact 路径（相对仓库根），为空时用默认路径
    automation_policy_path: str = ""

    rq_queue_name: str = "issueflow"
    agent_job_timeout_seconds: int = 180
    command_job_timeout_seconds: int = 120
    rq_max_retries: int = 2
    rq_retry_intervals: str = "10,30"
    outbox_max_attempts: int = 5
    outbox_base_backoff_seconds: int = 5
    llm_input_cost_per_million_usd: float | None = None
    llm_output_cost_per_million_usd: float | None = None

    embedding_provider: str = "disabled"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = Field(
        default=384,
        validation_alias=AliasChoices("EMBEDDING_DIMENSION", "EMBEDDING_DIMENSIONS"),
    )
    embedding_batch_size: int = 16
    embedding_query_prefix: str = ""
    embedding_cache_dir: str = "/var/cache/issueflow/fastembed"
    embedding_local_files_only: bool = False
    duplicate_top_k: int = 5
    duplicate_rrf_k: int = 60
    duplicate_min_score: float = 0.0
    duplicate_reranker_enabled: bool = False
    embedding_chunk_size: int = 384
    embedding_chunk_overlap: int = 64
    embedding_max_chunks: int = 16
    embedding_chunk_strategy_version: str = "title-body-token-v1"
    embedding_chunk_aggregation: Literal["max_chunk_score", "mean_top2_chunk_score"] = (
        "max_chunk_score"
    )
    eval_repos: str = "microsoft/vscode,nodejs/node,rust-lang/rust"
    eval_corpus_limit_per_repo: int = 2000
    eval_query_limit_per_repo: int = 50

    @property
    def evaluation_repositories(self) -> list[str]:
        return [item.strip() for item in self.eval_repos.split(",") if item.strip()]

    @property
    def embedding_dimensions(self) -> int:
        """Compatibility alias for the milestone-three internal name."""
        return self.embedding_dimension

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
