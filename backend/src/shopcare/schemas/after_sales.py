from datetime import date, datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class AfterSalesDraftCreateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    order_id: str = Field(min_length=1, max_length=50)
    item_id: str = Field(min_length=1, max_length=50)
    request_type: Literal["return", "exchange"]
    reason: str = Field(min_length=1, max_length=1000)
    as_of: date


class AfterSalesDraftResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
    )

    id: str
    order_id: str
    item_id: str
    reason: str
    request_type: Literal["return", "exchange"]
    status: Literal["pending", "cancelled", "submitted"] = Field(
        validation_alias=AliasChoices("public_status", "status"),
    )
    version: int = Field(ge=1)
    as_of: date
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class AfterSalesDraftCancelRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    expected_version: int = Field(
        ge=1,
        strict=True,
    )


class AfterSalesFacts(BaseModel):
    """User declarations for admission checks, never verified inspection findings."""

    model_config = ConfigDict(extra="forbid", strict=True)

    issue_type: Literal["malfunction", "damage", "wrong_spec"] | None = None
    unopened: bool | None = None
    unused: bool | None = None
    accessories_complete: bool | None = None
    packaging_complete: bool | None = None


class AfterSalesConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    confirmed: bool = Field(strict=True)
    facts: AfterSalesFacts


class AfterSalesApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    draft_id: str
    confirmed_version: int
    status: Literal["pending_review"]
    snapshot: dict
    created_at: datetime


class AfterSalesPrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order_id: str = Field(min_length=1, max_length=50)
    item_id: str = Field(min_length=1, max_length=50)
    request_type: Literal["return", "exchange"]
    reason: str = Field(min_length=1, max_length=1000)
    facts: AfterSalesFacts
