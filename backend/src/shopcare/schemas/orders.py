from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
