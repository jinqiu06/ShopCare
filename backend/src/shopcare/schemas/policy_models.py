from datetime import date
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    effective_from: date
    effective_until: date | None
    content: str = Field(min_length=1)

    @field_validator("policy_id", "version", "title", "category", "content")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能只包含空白字符")

        return value

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("失效日期必须晚于生效日期")

        return self


class PolicyChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    policy_id: str
    version: str
    title: str
    category: str
    effective_from: date
    effective_until: date | None
    text: str
    start_char: int
    end_char: int


class PolicySearchHit(BaseModel):
    chunk: PolicyChunk
    score: int = Field(ge=1)
