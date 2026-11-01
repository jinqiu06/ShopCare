from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolicyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "answered",
        "needs_clarification",
        "insufficient_evidence",
    ]
    answer: str = Field(min_length=1)
    citation_chunk_ids: list[str]
    limitations: list[str]


class OrderAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    as_of: date

    @field_validator("item_id", "query")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value
