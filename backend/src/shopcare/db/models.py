from datetime import date, datetime

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shopcare.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )


class Order(Base):
    __tablename__ = "orders"

    __table_args__ = (
        CheckConstraint(
            "total_amount_cents >= 0",
            name="ck_orders_amount_nonnegative",
        ),
        CheckConstraint(
            "status IN ('created', 'paid', 'shipped', 'delivered', 'cancelled')",
            name="ck_orders_status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    total_amount_cents: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    __table_args__ = (
        CheckConstraint(
            "quantity > 0",
            name="ck_order_items_quantity_positive",
        ),
        CheckConstraint(
            "unit_price_cents >= 0",
            name="ck_order_items_price_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    order_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("orders.id"),
        nullable=False,
        index=True,
    )
    product_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    unit_price_cents: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


class PolicyChunkRecord(Base):
    __tablename__ = "policy_chunks"

    __table_args__ = (
        CheckConstraint(
            "start_char >= 0",
            name="ck_policy_chunks_start_nonnegative",
        ),
        CheckConstraint(
            "end_char > start_char",
            name="ck_policy_chunks_range_valid",
        ),
    )

    chunk_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    policy_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    effective_from: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    effective_until: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    start_char: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    end_char: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


class PolicyEmbeddingRecord(Base):
    __tablename__ = "policy_embeddings"

    __table_args__ = (
        CheckConstraint(
            "dimensions > 0",
            name="ck_policy_embeddings_dimensions_positive",
        ),
    )

    chunk_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("policy_chunks.chunk_id", ondelete="CASCADE"),
        primary_key=True,
    )

    model_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    dimensions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    vector: Mapped[list[float]] = mapped_column(
        JSON,
        nullable=False,
    )
