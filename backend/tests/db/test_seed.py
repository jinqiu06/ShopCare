import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import Order, OrderItem, User
from shopcare.scripts.seed import seed_demo_data


@pytest.fixture
def engine(tmp_path):
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'seed.db').as_posix()}")
    init_db(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def counts(session):
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (User, Order, OrderItem)
    )


def populate(engine):
    with Session(engine) as session, session.begin():
        return seed_demo_data(session)


def test_seed_complete_dataset(engine):
    assert populate(engine) == {
        "users_created": 2,
        "orders_created": 20,
        "order_items_created": 40,
    }
    with Session(engine) as session:
        assert counts(session) == (2, 20, 40)
        orders = session.scalars(select(Order)).all()
        assert Counter(order.user_id for order in orders) == {"U001": 10, "U002": 10}
        assert Counter(order.status for order in orders) == {
            "created": 4,
            "paid": 4,
            "shipped": 4,
            "delivered": 4,
            "cancelled": 4,
        }
        for order in orders:
            items = session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
            assert len(items) == 2
            assert order.total_amount_cents == sum(
                item.quantity * item.unit_price_cents for item in items
            )
            assert (order.delivered_at is not None) == (order.status == "delivered")
            if order.delivered_at:
                assert order.delivered_at >= order.created_at


def test_seed_idempotent_across_new_connections(engine):
    populate(engine)
    engine.dispose()
    assert populate(engine) == {
        "users_created": 0,
        "orders_created": 0,
        "order_items_created": 0,
    }
    with Session(engine) as session:
        assert counts(session) == (2, 20, 40)


def test_seed_leaves_commit_to_caller(engine):
    with Session(engine) as session:
        seed_demo_data(session)
        assert counts(session) == (2, 20, 40)
        session.rollback()
    with Session(engine) as session:
        assert counts(session) == (0, 0, 0)


def test_seed_preserves_existing_records(engine):
    populate(engine)
    with Session(engine) as session, session.begin():
        session.get(User, "U001").display_name = "updated by user"
    populate(engine)
    with Session(engine) as session:
        assert session.get(User, "U001").display_name == "updated by user"


def test_seed_restores_missing_item_without_duplicates(engine):
    populate(engine)
    with Session(engine) as session, session.begin():
        item = session.scalar(select(OrderItem).order_by(OrderItem.id))
        session.delete(item)
    assert populate(engine) == {
        "users_created": 0,
        "orders_created": 0,
        "order_items_created": 1,
    }
    with Session(engine) as session:
        assert counts(session) == (2, 20, 40)


def test_seed_cli_twice(tmp_path):
    database = tmp_path / "nested" / "demo.db"
    url = f"sqlite:///{database.as_posix()}"
    project_root = Path(__file__).resolve().parents[3]
    command = [sys.executable, "-m", "shopcare.scripts.seed", "--database-url", url]
    for expected in (
        {"users_created": 2, "orders_created": 20, "order_items_created": 40},
        {"users_created": 0, "orders_created": 0, "order_items_created": 0},
    ):
        result = subprocess.run(
            command, cwd=project_root, text=True, capture_output=True, encoding="utf-8", timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == expected
    db_engine = build_engine(url)
    try:
        with Session(db_engine) as session:
            assert counts(session) == (2, 20, 40)
    finally:
        db_engine.dispose()
