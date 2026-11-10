from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    as_of: date


class ConversationResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
    )

    id: str
    status: Literal[
        "ready",
        "running",
        "failed",
    ]
    as_of: date


class ConversationMessageRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    query: str = Field(
        min_length=1,
        max_length=1000,
    )

    @field_validator("query")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value
