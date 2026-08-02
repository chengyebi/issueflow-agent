from pydantic import BaseModel, Field


class DuplicateEvalQuery(BaseModel):
    repo: str
    issue_number: int
    title: str
    body: str = ""


class DuplicateEvalExpected(BaseModel):
    relevant_issue_numbers: list[int] = Field(default_factory=list)
    is_duplicate: bool


class DuplicateEvalCase(BaseModel):
    id: str
    query: DuplicateEvalQuery
    expected: DuplicateEvalExpected
    source: str


class DuplicateEvalPrediction(BaseModel):
    case_id: str
    mode: str
    ranked_issue_numbers: list[int]
    predicted_is_duplicate: bool
    predicted_candidate_issue_number: int | None = None
    latency_ms: float = Field(ge=0)
    degraded: bool = False
