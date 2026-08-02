from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HistoricalIssue(BaseModel):
    repo: str
    issue_number: int = Field(gt=0)
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    state: Literal["open", "closed"]
    github_updated_at: datetime


class SearchHit(BaseModel):
    historical_issue_id: int
    repo: str
    issue_number: int
    title: str
    body: str
    state: str
    score: float


class SimilarIssueCandidate(BaseModel):
    historical_issue_id: int
    repo: str
    issue_number: int
    title: str
    state: str
    lexical_score: float | None = None
    vector_score: float | None = None
    rrf_score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    sources: list[Literal["lexical", "vector"]]
    evidence: str


class RetrievalResult(BaseModel):
    mode: Literal["lexical", "vector", "hybrid"]
    degraded: bool = False
    degradation_reason: str | None = None
    candidates: list[SimilarIssueCandidate]
