from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.db.models import Order, OrderItem
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.orders import (
    get_order_for_user,
    get_order_items_for_user,
    list_orders_for_user,
)
from shopcare.tools.executor import execute_tool
from shopcare.tools.orders import build_order_tools
from shopcare.tools.policy import build_policy_tools


@pytest.mark.parametrize("item_id,expected", [("O004-I01", "digital"), ("O004-I02", "accessory")])
def test_policy_tool_executor_real_ownership_and_retrieval(
    session, answer_clients, item_id, expected
):
    from datetime import date

    embedding, chat = answer_clients
    entries = build_order_tools(session, "U001", date(2026, 11, 2)) + build_policy_tools(
        session, embedding, "U001", date(2026, 11, 2)
    )
    tools = {entry.name: entry for entry in entries}
    result = execute_tool(
        tools,
        "search_order_policies",
        {"order_id": " O004 ", "item_id": f" {item_id} ", "query": " 换货 "},
    )
    assert result["ok"] is True
    assert [hit["chunk"]["chunk_id"] for hit in result["data"]] == [expected]
    embedding.embed_query.assert_called_once_with("换货")
    chat.invoke.assert_not_called()


@pytest.mark.parametrize(
    "order_id,item_id", [("missing", "missing"), ("O011", "O011-I01"), ("O004", "O001-I01")]
)
def test_policy_tool_denied_context_never_embeds(session, monkeypatch, order_id, item_id):
    from datetime import date
    from unittest.mock import Mock

    from shopcare.tools import policy

    embedding, search = Mock(), Mock()
    monkeypatch.setattr(policy, "search_semantic_policy_chunks", search)
    entries = build_policy_tools(session, embedding, "U001", date(2026, 11, 2))
    result = execute_tool(
        {entry.name: entry for entry in entries},
        "search_order_policies",
        {"order_id": order_id, "item_id": item_id, "query": "换货"},
    )
    assert result == {
        "ok": False,
        "error": {"code": "ORDER_CONTEXT_NOT_FOUND", "message": "订单或商品不存在或不可访问"},
    }
    search.assert_not_called()
    embedding.embed_query.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"user_id": "U002"},
        {"as_of": "2020-01-01"},
        {"category": "配件"},
        {"item_id": 123},
        {"query": "x" * 1001},
    ],
)
def test_policy_tool_executor_rejects_untrusted_context_and_bad_input(monkeypatch, override):
    from datetime import date
    from unittest.mock import Mock

    from shopcare.tools import policy

    context, embedding = Mock(), Mock()
    monkeypatch.setattr(policy, "get_order_answer_context", context)
    entries = build_policy_tools(Mock(spec=Session), embedding, "U001", date(2026, 11, 2))
    arguments = {"order_id": "O004", "item_id": "O004-I01", "query": "换货"}
    arguments.update(override)
    result = execute_tool(
        {entry.name: entry for entry in entries}, "search_order_policies", arguments
    )
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    context.assert_not_called()
    embedding.embed_query.assert_not_called()


@pytest.mark.parametrize("failure", ["database", "missing_index", "timeout", "upstream"])
def test_policy_tool_failures_are_safe_and_never_call_chat(
    session, answer_clients, monkeypatch, failure
):
    from datetime import date

    from openai import OpenAIError
    from sqlalchemy import delete
    from sqlalchemy.exc import SQLAlchemyError

    from shopcare.db.models import PolicyEmbeddingRecord
    from shopcare.tools import policy

    embedding, chat = answer_clients
    if failure == "database":

        def fail_context(*args):
            raise SQLAlchemyError("private database detail")

        monkeypatch.setattr(policy, "get_order_answer_context", fail_context)
    elif failure == "missing_index":
        session.execute(delete(PolicyEmbeddingRecord))
        session.flush()
    else:
        embedding.embed_query.side_effect = (
            TimeoutError("private timeout") if failure == "timeout" else OpenAIError("private key")
        )
    entries = build_policy_tools(session, embedding, "U001", date(2026, 11, 2))
    result = execute_tool(
        {entry.name: entry for entry in entries},
        "search_order_policies",
        {"order_id": "O004", "item_id": "O004-I01", "query": "换货"},
    )
    assert result == {
        "ok": False,
        "error": {"code": "RETRIEVAL_UNAVAILABLE", "message": "政策检索暂时不可用"},
    }
    chat.invoke.assert_not_called()
    if failure in ("database", "missing_index"):
        embedding.embed_query.assert_not_called()


def test_tool_executor_dispatches_real_order_tool(session):
    tools = {entry.name: entry for entry in build_order_tools(session, "U001", date(2026, 11, 2))}
    result = execute_tool(tools, "lookup_order", {"order_id": "O004"})
    assert result["ok"] is True
    assert result["data"]["id"] == "O004"
    assert execute_tool(tools, "lookup_order", {"order_id": "O011"})["error"]["code"] == (
        "ORDER_NOT_FOUND"
    )


def test_tool_executor_unknown_tool_does_not_invoke_registered_tool():
    from unittest.mock import Mock

    registered = Mock()
    assert execute_tool({"lookup_order": registered}, "private_unknown", {}) == {
        "ok": False,
        "error": {"code": "UNKNOWN_TOOL", "message": "工具不存在"},
    }
    registered.invoke.assert_not_called()
    registered.get_input_schema.assert_not_called()


@pytest.mark.parametrize(
    "arguments", [{}, {"order_id": 123}, {"order_id": "O004", "user_id": "U002"}]
)
def test_tool_executor_converts_input_errors_before_invocation(arguments):
    from unittest.mock import Mock

    from shopcare.schemas.orders import OrderLookupInput

    selected = Mock()
    selected.get_input_schema.return_value = OrderLookupInput
    assert execute_tool({"lookup_order": selected}, "lookup_order", arguments) == {
        "ok": False,
        "error": {"code": "INVALID_ARGUMENT", "message": "工具参数不合法"},
    }
    selected.invoke.assert_not_called()


def test_tool_executor_does_not_misclassify_internal_validation_error():
    from unittest.mock import Mock

    from shopcare.schemas.orders import OrderLookupInput, OrderResponse

    try:
        OrderResponse.model_validate({})
    except ValidationError as error:
        internal_error = error
    selected = Mock()
    selected.get_input_schema.return_value = OrderLookupInput
    selected.invoke.side_effect = internal_error
    with pytest.raises(ValidationError) as captured:
        execute_tool({"lookup_order": selected}, "lookup_order", {"order_id": "O004"})
    assert captured.value is internal_error


@pytest.mark.parametrize(
    "user_id,order_id,expected_code",
    [
        ("U001", " O004 ", None),
        ("U002", "O011", None),
        ("U001", "O011", "ORDER_NOT_FOUND"),
        ("U001", "missing", "ORDER_NOT_FOUND"),
        ("U001", "   ", "INVALID_ARGUMENT"),
    ],
)
def test_order_tool_invocation_and_safe_output(session, user_id, order_id, expected_code):
    import json

    lookup = build_order_tools(session, user_id, date(2026, 11, 2))[0]
    result = lookup.invoke({"order_id": order_id})
    json.dumps(result, allow_nan=False)
    if expected_code:
        assert result == {
            "ok": False,
            "error": {
                "code": expected_code,
                "message": "订单号不能为空"
                if expected_code == "INVALID_ARGUMENT"
                else "订单不存在或不可访问",
            },
        }
    else:
        assert result["ok"] is True
        assert result["data"]["id"] == order_id.strip()
        assert set(result["data"]) == {
            "id",
            "status",
            "total_amount_cents",
            "created_at",
            "delivered_at",
            "as_of",
            "days_since_delivery",
        }


def test_order_tool_schema_and_separate_user_bindings(session):
    first = build_order_tools(session, "U001", date(2026, 11, 2))[0]
    second = build_order_tools(session, "U002", date(2026, 11, 2))[0]
    assert set(first.args) == {"order_id"}
    assert first.tool_call_schema.model_json_schema()["required"] == ["order_id"]
    assert second.invoke({"order_id": "O011"})["ok"] is True
    # Strict tool inputs reject client-supplied identity before execution.
    with pytest.raises(ValidationError):
        first.invoke({"order_id": "O011", "user_id": "U002"})
    denied = first.invoke({"order_id": "O011"})
    missing = first.invoke({"order_id": "missing"})
    assert denied == missing
    assert denied["error"]["code"] == "ORDER_NOT_FOUND"


@pytest.mark.parametrize(
    "order_id,as_of,expected",
    [
        ("O004", date(2026, 10, 30), 0),
        ("O004", date(2026, 11, 2), 3),
        ("O004", date(2026, 11, 15), 16),
        ("O001", date(2026, 11, 2), None),
    ],
)
def test_order_tool_returns_server_computed_delivery_days(session, order_id, as_of, expected):
    lookup = build_order_tools(session, "U001", as_of)[0]
    result = lookup.invoke({"order_id": order_id})
    assert result["ok"] is True
    assert result["data"]["as_of"] == as_of.isoformat()
    assert result["data"]["days_since_delivery"] == expected
    with pytest.raises(ValidationError):
        lookup.invoke({"order_id": order_id, "as_of": "2030-01-01"})


def test_order_tool_refuses_reference_date_before_delivery(session):
    lookup = build_order_tools(session, "U001", date(2026, 10, 29))[0]
    result = lookup.invoke({"order_id": "O004"})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_REFERENCE_DATE"
    assert "data" not in result


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"order_id": 123},
        {"order_id": None},
        {"order_id": ""},
        {"order_id": "x" * 101},
        {"order_id": "O004", "user_id": "U002"},
    ],
)
def test_order_tool_rejects_invalid_schema_before_query(monkeypatch, arguments):
    from unittest.mock import Mock

    from shopcare.tools import orders as order_tools

    query = Mock(side_effect=AssertionError("invalid input must not query database"))
    monkeypatch.setattr(order_tools, "get_order_for_user", query)
    lookup = build_order_tools(Mock(spec=Session), "U001", date(2026, 11, 2))[0]
    with pytest.raises(ValidationError):
        lookup.invoke(arguments)
    query.assert_not_called()


def test_order_tool_database_failure_is_safe_and_keeps_transaction_ownership(monkeypatch):
    from unittest.mock import Mock

    from sqlalchemy.exc import SQLAlchemyError

    from shopcare.tools import orders as order_tools

    db_session = Mock(spec=Session)
    query = Mock(side_effect=SQLAlchemyError("private SQL and connection detail"))
    monkeypatch.setattr(order_tools, "get_order_for_user", query)
    lookup = build_order_tools(db_session, "U001", date(2026, 11, 2))[0]
    assert lookup.invoke({"order_id": " O004 "}) == {
        "ok": False,
        "error": {
            "code": "TEMPORARILY_UNAVAILABLE",
            "message": "订单查询暂时不可用，请稍后重试",
        },
    }
    query.assert_called_once_with(db_session, "U001", "O004")
    db_session.commit.assert_not_called()
    db_session.rollback.assert_not_called()
    db_session.close.assert_not_called()


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
