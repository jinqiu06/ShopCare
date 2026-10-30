from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import Order, OrderItem, User


@pytest.fixture
def engine(tmp_path):
    db_engine = build_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def make_order(**overrides):
    values = dict(
        id="O001",
        user_id="U001",
        status="paid",
        total_amount_cents=3000,
        created_at=datetime(2026, 10, 26),
    )
    values.update(overrides)
    return Order(**values)


def make_item(**overrides):
    values = dict(
        id="I001",
        order_id="O001",
        product_name="demo item",
        category="accessories",
        quantity=2,
        unit_price_cents=1500,
    )
    values.update(overrides)
    return OrderItem(**values)


def add_order(session):
    session.add(User(id="U001", display_name="demo user"))
    session.flush()
    session.add(make_order())
    session.flush()


def test_complete_order_persists_across_connections(engine):
    with Session(engine) as session, session.begin():
        add_order(session)
        session.add(make_item())
    engine.dispose()
    with Session(engine) as session:
        order = session.get(Order, "O001")
        item = session.get(OrderItem, "I001")
        assert session.get(User, "U001") is not None
        assert order.user_id == "U001"
        assert item.order_id == order.id
        assert order.total_amount_cents == item.quantity * item.unit_price_cents


@pytest.mark.parametrize("record", ["order", "item"])
def test_foreign_keys_reject_missing_parent(engine, record):
    # Force a fresh connection so enforcement cannot rely on the DDL connection.
    engine.dispose()
    invalid = make_order() if record == "order" else make_item()
    with Session(engine) as session:
        session.add(invalid)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_amount_cents": -1},
        {"status": "unknown"},
    ],
)
def test_order_constraints(engine, overrides):
    with Session(engine) as session:
        session.add(User(id="U001", display_name="demo user"))
        session.flush()
        session.add(make_order(**overrides))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": 0},
        {"quantity": -1},
        {"unit_price_cents": -1},
    ],
)
def test_item_constraints(engine, overrides):
    with Session(engine) as session:
        add_order(session)
        session.add(make_item(**overrides))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_failure_rolls_back_flushed_parents(engine):
    with Session(engine) as session:
        add_order(session)
        session.add(make_item(quantity=0))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    with Session(engine) as session:
        assert session.get(User, "U001") is None
        assert session.get(Order, "O001") is None
        assert session.get(OrderItem, "I001") is None


def test_init_registers_models_in_fresh_process():
    import json
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import json; from sqlalchemy import inspect; "
        "from shopcare.db import build_engine, init_db; "
        "engine = build_engine('sqlite:///:memory:'); init_db(engine); "
        "print(json.dumps(sorted(inspect(engine).get_table_names()))); engine.dispose()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "order_items",
        "orders",
        "policy_chunks",
        "policy_embeddings",
        "users",
    ]
