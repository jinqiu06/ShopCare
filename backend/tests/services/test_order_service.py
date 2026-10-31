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


@pytest.mark.parametrize("item_id", ["O001-I01", "O001-I02"])
def test_answer_context_selects_requested_item(session, item_id):
    from shopcare.services.order_answers import get_order_answer_context

    order, item = get_order_answer_context(session, "U001", "O001", item_id)
    assert order.id == "O001"
    assert item.id == item_id
    assert item.category in ("数码", "配件")
    assert "user_id" not in order.model_dump()
    assert order.model_dump(mode="json")["created_at"]


@pytest.mark.parametrize(
    "user_id,order_id",
    [
        ("U002", "O001"),
        ("U001", "missing"),
        ("unknown", "O001"),
    ],
)
def test_answer_context_inaccessible_order_has_same_error(session, user_id, order_id):
    from unittest.mock import patch

    from shopcare.services.order_answers import get_order_answer_context

    with patch("shopcare.services.order_answers.get_order_items_for_user") as read_items:
        with pytest.raises(LookupError, match="^订单不存在或不可访问$"):
            get_order_answer_context(session, user_id, order_id, "O001-I01")
        read_items.assert_not_called()


@pytest.mark.parametrize("item_id", ["missing", "O002-I01"])
def test_answer_context_rejects_item_from_other_order(session, item_id):
    from shopcare.services.order_answers import get_order_answer_context

    with pytest.raises(LookupError, match="^指定商品不在该订单中$"):
        get_order_answer_context(session, "U001", "O001", item_id)


def test_answer_context_empty_order_items_raise(session):
    from sqlalchemy import delete

    from shopcare.services.order_answers import get_order_answer_context

    session.execute(delete(OrderItem).where(OrderItem.order_id == "O001"))
    with pytest.raises(LookupError, match="^指定商品不在该订单中$"):
        get_order_answer_context(session, "U001", "O001", "O001-I01")


@pytest.fixture
def answer_clients(session):
    import json
    from datetime import date
    from unittest.mock import Mock

    from langchain_core.messages import AIMessage

    from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord

    for category, name in [("数码", "digital"), ("配件", "accessory")]:
        session.add(
            PolicyChunkRecord(
                chunk_id=name,
                policy_id=name,
                version="v1",
                title="模拟换货",
                category=category,
                effective_from=date(2026, 1, 1),
                text="模拟政策",
                start_char=0,
                end_char=4,
            )
        )
    session.flush()
    for name in ["digital", "accessory"]:
        session.add(
            PolicyEmbeddingRecord(
                chunk_id=name,
                model_id="test-model",
                dimensions=2,
                vector=[1.0, 0.0],
            )
        )
    session.flush()
    embedding, chat = Mock(), Mock()
    embedding.model, embedding.dimensions = "test-model", 2
    embedding.embed_query.return_value = [1.0, 0.0]

    def reply(messages):
        data = json.loads(messages[1].content)
        return AIMessage(
            content=json.dumps(
                {
                    "status": "answered",
                    "answer": "请按模拟政策核实故障。",
                    "citation_chunk_ids": [data["evidence"][0]["chunk_id"]],
                    "limitations": ["尚未核实故障"],
                }
            ),
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    chat.invoke.side_effect = reply
    return embedding, chat


@pytest.mark.parametrize("item_id,expected", [("O004-I01", "digital"), ("O004-I02", "accessory")])
def test_answer_pipeline_uses_selected_category_and_real_core(
    session, answer_clients, item_id, expected
):
    import json
    from datetime import date

    from shopcare.services.order_answers import answer_order_question

    embedding, chat = answer_clients
    answer, raw = answer_order_question(
        session, embedding, chat, "U001", "O004", item_id, "可以换货吗", date(2026, 11, 2)
    )
    assert answer.citation_chunk_ids == [expected]
    assert raw.usage_metadata["total_tokens"] == 15
    embedding.embed_query.assert_called_once()
    chat.invoke.assert_called_once()
    data = json.loads(chat.invoke.call_args.args[0][1].content)
    assert [item["id"] for item in data["items"]] == [item_id]


@pytest.mark.parametrize(
    "user,order,item,query,error",
    [
        ("U002", "O004", "O004-I01", "换货", LookupError),
        ("U001", "missing", "missing", "换货", LookupError),
        ("U001", "O004", "O001-I01", "换货", LookupError),
        ("U001", "O004", "O004-I01", "  ", ValueError),
    ],
)
def test_answer_pipeline_invalid_request_never_calls_models(
    session, user, order, item, query, error
):
    from datetime import date
    from unittest.mock import Mock

    from shopcare.services.order_answers import answer_order_question

    embedding, chat = Mock(), Mock()
    with pytest.raises(error):
        answer_order_question(session, embedding, chat, user, order, item, query, date(2026, 11, 2))
    embedding.embed_query.assert_not_called()
    chat.invoke.assert_not_called()


def test_answer_pipeline_embedding_failure_prevents_chat(session, answer_clients):
    from datetime import date

    from shopcare.services.order_answers import answer_order_question

    embedding, chat = answer_clients
    embedding.embed_query.side_effect = TimeoutError("simulated timeout")
    with pytest.raises(TimeoutError):
        answer_order_question(
            session, embedding, chat, "U001", "O004", "O004-I01", "换货", date(2026, 11, 2)
        )
    chat.invoke.assert_not_called()
