"""Stable RQ import path for background tasks."""

from app.rag.indexing import embed_historical_issue
from app.tasks import (
    process_github_command,
    process_issue_agent_run,
    process_review_commands,
)

process_issue_embedding = embed_historical_issue

__all__ = [
    "process_github_command",
    "process_issue_agent_run",
    "process_review_commands",
    "process_issue_embedding",
]
