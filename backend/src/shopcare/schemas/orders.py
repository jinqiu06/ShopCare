from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    total_amount_cents: int
    created_at: datetime
    delivered_at: datetime | None


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_name: str
    category: str
    quantity: int
    unit_price_cents: int


class OrderLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str = Field(
        min_length=1,
        max_length=100,
        description="需要查询的订单号，例如 O004",
    )
