from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
