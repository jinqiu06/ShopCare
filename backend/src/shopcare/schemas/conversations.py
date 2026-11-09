from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


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
