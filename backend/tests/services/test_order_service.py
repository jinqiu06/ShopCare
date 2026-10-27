import pytest
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import Order, OrderItem
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.orders import (
    get_order_for_user,
    get_order_items_for_user,
    list_orders_for_user,
)


@pytest.fixture
def session(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'orders.db').as_posix()}")
    init_db(engine)
    try:
        with Session(engine) as seed_session, seed_session.begin():
            seed_demo_data(seed_session)
        with Session(engine) as query_session:
            yield query_session
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "user_id,order_id,found",
    [
        ("U001", "O001", True),
        ("U002", "O001", False),
        ("U001", "missing", False),
        ("unknown", "O001", False),
    ],
)
def test_get_order_is_scoped_to_user(session, user_id, order_id, found):
    result = get_order_for_user(session, user_id, order_id)
    if found:
        assert isinstance(result, Order)
        assert result.id == order_id
        assert result.user_id == user_id
    else:
        assert result is None


@pytest.mark.parametrize(
    "user_id,start,end",
    [
        ("U001", 1, 11),
        ("U002", 11, 21),
        ("unknown", 1, 1),
    ],
)
def test_list_returns_all_owned_orders_in_order(session, user_id, start, end):
    orders = list_orders_for_user(session, user_id)
    assert isinstance(orders, list)
    assert all(isinstance(order, Order) for order in orders)
    assert [order.id for order in orders] == [f"O{i:03d}" for i in range(start, end)]
    assert all(order.user_id == user_id for order in orders)


@pytest.mark.parametrize(
    "user_id,order_id,allowed",
    [
        ("U001", "O001", True),
        ("U002", "O011", True),
        ("U002", "O001", False),
        ("U001", "missing", False),
    ],
)
def test_order_items_are_scoped_to_user(session, user_id, order_id, allowed):
    items = get_order_items_for_user(session, user_id, order_id)
    assert isinstance(items, list)
    if allowed:
        assert all(isinstance(item, OrderItem) for item in items)
        assert [item.id for item in items] == [f"{order_id}-I01", f"{order_id}-I02"]
        assert all(item.order_id == order_id for item in items)
    else:
        assert items == []


@pytest.mark.parametrize(
    "user_id,limit,offset,expected_ids",
    [
        ("U001", 3, 0, ["O001", "O002", "O003"]),
        ("U001", 3, 3, ["O004", "O005", "O006"]),
        ("U001", 3, 10, []),
        ("U002", 3, 3, ["O014", "O015", "O016"]),
        ("U001", 100, 0, [f"O{i:03d}" for i in range(1, 11)]),
    ],
)
def test_order_list_pagination(session, user_id, limit, offset, expected_ids):
    orders = list_orders_for_user(session, user_id, limit=limit, offset=offset)
    assert [order.id for order in orders] == expected_ids
    assert all(order.user_id == user_id for order in orders)


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (101, 0), (20, -1)])
def test_order_list_pagination_rejects_invalid_range(session, limit, offset):
    with pytest.raises(ValueError):
        list_orders_for_user(session, "U001", limit=limit, offset=offset)
