from typing import Literal

from pydantic import BaseModel, Field

from app.agents.workflow import Category, Priority, RiskLevel


class EvalIssue(BaseModel):
    repo: str
    issue_number: int
    title: str
    body: str


class ExpectedLabels(BaseModel):
    category: Category
    priority: Priority
    risk_level: RiskLevel


class EvalMetadata(BaseModel):
    source: str
    annotator: str | None = None
    notes: str | None = None


class EvalCase(BaseModel):
    id: str
    input: EvalIssue
    expected: ExpectedLabels
    metadata: EvalMetadata


class EvalPrediction(BaseModel):
    case_id: str
    category: Category | None = None
    priority: Priority | None = None
    risk_level: RiskLevel | None = None
    structured_output_success: bool
    agent_success: bool
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = None
    error_type: str | None = None


RunnerType = Literal["heuristic", "live"]
