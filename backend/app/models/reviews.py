from pydantic import BaseModel, Field


class ReviewDecisionRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    review_note: str | None = Field(default=None, max_length=4000)

