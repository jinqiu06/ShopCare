import json
from datetime import date
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from httpx import Request
from langchain_core.messages import AIMessage
from openai import APITimeoutError, OpenAIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api import orders as orders_api
from shopcare.api.dependencies import get_current_user_id
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.main import app, settings
from shopcare.schemas.answers import PolicyAnswer
from shopcare.scripts.seed import seed_demo_data


def test_answer_api_forwards_trusted_identity_and_returns_only_answer(client, monkeypatch):
    embedding, chat = object(), object()
    monkeypatch.setattr(orders_api, "build_embedding_client", Mock(return_value=embedding))
    monkeypatch.setattr(orders_api, "build_chat_client", Mock(return_value=chat))
    answer = PolicyAnswer(
        status="needs_clarification",
        answer="请描述具体故障，目前未创建申请。",
        citation_chunk_ids=[],
        limitations=["故障待确认"],
    )
    pipeline = Mock(return_value=(answer, object()))
    monkeypatch.setattr(orders_api, "answer_order_question", pipeline)
    response = client.post(
        "/api/orders/O004/answer?user_id=U002",
        headers={"X-User-ID": "U002"},
        json={"item_id": " O004-I01 ", "query": " 耳机坏了 ", "as_of": "2026-11-02"},
    )
    assert response.status_code == 200
    assert response.json() == answer.model_dump(mode="json")
    pipeline.assert_called_once()
    args = pipeline.call_args.args
    assert isinstance(args[0], Session)
    assert args[1:] == (embedding, chat, "U001", "O004", "O004-I01", "耳机坏了", date(2026, 11, 2))


@pytest.mark.parametrize(
    "override",
    [
        {"query": "   "},
        {"item_id": " "},
        {"as_of": "invalid"},
        {"query": "x" * 1001},
        {"user_id": "U002"},
    ],
)
def test_answer_api_rejects_invalid_body_before_clients(client, monkeypatch, override):
    embedding = Mock(side_effect=AssertionError("invalid body must not build clients"))
    chat = Mock(side_effect=AssertionError("invalid body must not build clients"))
    monkeypatch.setattr(orders_api, "build_embedding_client", embedding)
    monkeypatch.setattr(orders_api, "build_chat_client", chat)
    body = {"item_id": "O004-I01", "query": "耳机坏了", "as_of": "2026-11-02"}
    body.update(override)
    response = client.post("/api/orders/O004/answer", json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": "请求参数格式不正确"}
    }
    embedding.assert_not_called()
    chat.assert_not_called()


@pytest.mark.parametrize(
    "order_id,item_id",
    [
        ("missing", "missing"),
        ("O011", "O011-I01"),
        ("O004", "O001-I01"),
    ],
)
def test_answer_api_missing_unowned_and_wrong_item_return_safe_404(
    client, monkeypatch, order_id, item_id
):
    # Keep the real answer pipeline and ownership checks; replace only external clients.
    embedding, chat = Mock(), Mock()
    monkeypatch.setattr(orders_api, "build_embedding_client", Mock(return_value=embedding))
    monkeypatch.setattr(orders_api, "build_chat_client", Mock(return_value=chat))
    response = client.post(
        f"/api/orders/{order_id}/answer?user_id=U002",
        headers={"X-User-ID": "U002"},
        json={"item_id": item_id, "query": "可以换货吗", "as_of": "2026-11-02"},
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "ORDER_CONTEXT_NOT_FOUND",
            "message": "订单或商品不存在或不可访问",
        }
    }
    embedding.embed_query.assert_not_called()
    chat.invoke.assert_not_called()


@pytest.mark.parametrize(
    "error,status,code,message",
    [
        (
            APITimeoutError(request=Request("POST", "https://example.invalid/private")),
            504,
            "ANSWER_TIMEOUT",
            "回答服务超时，请稍后重试",
        ),
        (TimeoutError("private timeout detail"), 504, "ANSWER_TIMEOUT", "回答服务超时，请稍后重试"),
        (
            OpenAIError("private provider detail"),
            502,
            "ANSWER_UPSTREAM_ERROR",
            "回答服务暂时不可用",
        ),
    ],
)
def test_answer_api_converts_provider_errors_without_leaking_details(
    client, monkeypatch, error, status, code, message
):
    monkeypatch.setattr(orders_api, "build_embedding_client", Mock())
    monkeypatch.setattr(orders_api, "build_chat_client", Mock())
    pipeline = Mock(side_effect=error)
    monkeypatch.setattr(orders_api, "answer_order_question", pipeline)
    response = client.post(
        "/api/orders/O004/answer",
        json={"item_id": "O004-I01", "query": "可以换货吗", "as_of": "2026-11-02"},
    )
    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": message}}
    assert "private" not in response.text
    pipeline.assert_called_once()


@pytest.mark.parametrize("failure_at", ["Settings", "build_embedding_client", "build_chat_client"])
def test_answer_api_configuration_failure_stops_before_pipeline(client, monkeypatch, failure_at):
    stages = {
        "Settings": Mock(),
        "build_embedding_client": Mock(),
        "build_chat_client": Mock(),
    }
    stages[failure_at].side_effect = ValueError("private configuration detail")
    for name, stage in stages.items():
        monkeypatch.setattr(orders_api, name, stage)
    pipeline = Mock()
    monkeypatch.setattr(orders_api, "answer_order_question", pipeline)
    response = client.post(
        "/api/orders/O004/answer",
        json={"item_id": "O004-I01", "query": "可以换货吗", "as_of": "2026-11-02"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "ANSWER_NOT_CONFIGURED", "message": "回答服务配置暂不可用"}
    }
    assert "private" not in response.text
    pipeline.assert_not_called()
    names = list(stages)
    for name in names[names.index(failure_at) + 1 :]:
        stages[name].assert_not_called()


def test_answer_api_database_failure_is_safe_error(client, monkeypatch):
    monkeypatch.setattr(orders_api, "build_embedding_client", Mock())
    monkeypatch.setattr(orders_api, "build_chat_client", Mock())
    monkeypatch.setattr(
        orders_api, "answer_order_question", Mock(side_effect=SQLAlchemyError("private sql"))
    )
    response = client.post(
        "/api/orders/O004/answer",
        json={"item_id": "O004-I01", "query": "可以换货吗", "as_of": "2026-11-02"},
    )
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "ANSWER_FAILED", "message": "暂时无法生成可靠回答"}
    }


@pytest.mark.parametrize("output", ["valid", "invalid_json", "foreign_citation", "missing_index"])
def test_answer_api_real_pipeline_with_simulated_models(client, monkeypatch, output):
    with Session(app.state.engine) as session, session.begin():
        session.add(
            PolicyChunkRecord(
                chunk_id="http-policy",
                policy_id="http-policy",
                version="v1",
                title="模拟换货",
                category="数码",
                effective_from=date(2026, 1, 1),
                text="模拟政策",
                start_char=0,
                end_char=4,
            )
        )
        session.flush()
        if output != "missing_index":
            session.add(
                PolicyEmbeddingRecord(
                    chunk_id="http-policy",
                    model_id="test-model",
                    dimensions=2,
                    vector=[1.0, 0.0],
                )
            )
    embedding, chat = Mock(), Mock()
    embedding.model, embedding.dimensions = "test-model", 2
    embedding.embed_query.return_value = [1.0, 0.0]
    payload = {
        "status": "answered",
        "answer": "请核实模拟政策中的换货条件。",
        "citation_chunk_ids": ["http-policy"],
        "limitations": ["尚未核实故障"],
    }
    if output == "foreign_citation":
        payload["citation_chunk_ids"] = ["private-foreign-policy"]
    chat.invoke.return_value = AIMessage(
        content="private invalid json" if output == "invalid_json" else json.dumps(payload)
    )
    monkeypatch.setattr(orders_api, "build_embedding_client", Mock(return_value=embedding))
    monkeypatch.setattr(orders_api, "build_chat_client", Mock(return_value=chat))
    response = client.post(
        "/api/orders/O004/answer",
        json={"item_id": "O004-I01", "query": "可以换货吗", "as_of": "2026-11-02"},
    )
    if output == "valid":
        assert response.status_code == 200
        assert response.json() == payload
        facts = json.loads(chat.invoke.call_args.args[0][1].content)
        assert facts["order"]["id"] == "O004"
        assert facts["items"][0]["id"] == "O004-I01"
        assert facts["days_since_delivery"] == 3
        assert [chunk["chunk_id"] for chunk in facts["evidence"]] == ["http-policy"]
    else:
        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "ANSWER_FAILED", "message": "暂时无法生成可靠回答"}
        }
    if output == "missing_index":
        embedding.embed_query.assert_not_called()
        chat.invoke.assert_not_called()
    else:
        embedding.embed_query.assert_called_once()
        chat.invoke.assert_called_once()


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = build_engine(url)
    init_db(engine)
    try:
        with Session(engine) as session, session.begin():
            seed_demo_data(session)
    finally:
        engine.dispose()
    previous_overrides = app.dependency_overrides.copy()
    try:
        # Exercise the real lifespan and request-level session dependency.
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.mark.parametrize(
    "user_id,start,end",
    [
        ("U001", 1, 11),
        ("U002", 11, 21),
        ("unknown", 1, 1),
    ],
)
def test_list_api_scoped_and_ordered(client, user_id, start, end):
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    response = client.get("/api/orders")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [f"O{i:03d}" for i in range(start, end)]


@pytest.mark.parametrize(
    "user_id,order_id,status",
    [
        ("U001", "O001", 200),
        ("U002", "O011", 200),
        ("U001", "O011", 404),
        ("U002", "O001", 404),
        ("U001", "missing", 404),
    ],
)
def test_detail_api_access_and_schema(client, user_id, order_id, status):
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    response = client.get(f"/api/orders/{order_id}")
    assert response.status_code == status
    payload = response.json()
    if status == 200:
        assert set(payload) == {
            "id",
            "status",
            "total_amount_cents",
            "created_at",
            "delivered_at",
        }
        assert payload["id"] == order_id
        assert payload["total_amount_cents"] == 22800
        assert isinstance(payload["created_at"], str)
        assert "user_id" not in payload
    else:
        assert payload == {
            "error": {
                "code": "ORDER_NOT_FOUND",
                "message": "订单不存在或不可访问",
            }
        }


def test_missing_and_unowned_order_have_identical_errors(client):
    missing = client.get("/api/orders/missing")
    unowned = client.get("/api/orders/O011")
    assert missing.status_code == unowned.status_code == 404
    assert missing.json() == unowned.json()


def test_client_cannot_switch_demo_identity(client):
    response = client.get("/api/orders?user_id=U002", headers={"X-User-ID": "U002"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [f"O{i:03d}" for i in range(1, 11)]
    assert client.get("/api/orders/O011?user_id=U002").status_code == 404


def test_unknown_route_error_shape(client):
    response = client.get("/api/unknown")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_method_not_allowed_preserves_allow_header(client):
    response = client.post("/api/orders")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_validation_error_does_not_echo_input():
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError

    from shopcare.api.errors import validation_error_handler

    isolated_app = FastAPI()
    isolated_app.add_exception_handler(RequestValidationError, validation_error_handler)

    @isolated_app.get("/validate/{value}")
    def validate(value: int):
        return {"value": value}

    with TestClient(isolated_app) as test_client:
        response = test_client.get("/validate/private-input")
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "请求参数格式不正确",
        }
    }
    assert "private-input" not in response.text


@pytest.mark.parametrize(
    "user_id,order_id,status",
    [
        ("U001", "O001", 200),
        ("U002", "O011", 200),
        ("U001", "O011", 404),
        ("U002", "O001", 404),
        ("U001", "missing", 404),
    ],
)
def test_items_api_access_and_schema(client, user_id, order_id, status):
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    response = client.get(f"/api/orders/{order_id}/items")
    assert response.status_code == status
    if status == 404:
        assert response.json() == {
            "error": {
                "code": "ORDER_NOT_FOUND",
                "message": "订单不存在或不可访问",
            }
        }
        return
    payload = response.json()
    assert isinstance(payload, list)
    assert [item["id"] for item in payload] == [f"{order_id}-I01", f"{order_id}-I02"]
    for item in payload:
        assert set(item) == {"id", "product_name", "category", "quantity", "unit_price_cents"}
    assert sum(item["quantity"] * item["unit_price_cents"] for item in payload) == 22800


def test_accessible_order_without_items_returns_empty_list(client):
    from sqlalchemy import delete

    from shopcare.db.models import OrderItem

    with Session(app.state.engine) as session, session.begin():
        session.execute(delete(OrderItem).where(OrderItem.order_id == "O001"))
    response = client.get("/api/orders/O001/items")
    assert response.status_code == 200
    assert response.json() == []
    assert client.get("/api/orders/O001").status_code == 200


def test_item_endpoint_ignores_client_identity_override(client):
    response = client.get("/api/orders/O011/items?user_id=U002", headers={"X-User-ID": "U002"})
    missing = client.get("/api/orders/missing/items")
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json()


@pytest.mark.parametrize(
    "user_id,params,expected_ids",
    [
        ("U001", {"limit": 3}, ["O001", "O002", "O003"]),
        ("U001", {"limit": 3, "offset": 3}, ["O004", "O005", "O006"]),
        ("U001", {"limit": 3, "offset": 10}, []),
        ("U002", {"limit": 3, "offset": 3}, ["O014", "O015", "O016"]),
        ("U001", {"limit": 100, "offset": 0}, [f"O{i:03d}" for i in range(1, 11)]),
    ],
)
def test_api_order_pagination(client, user_id, params, expected_ids):
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    response = client.get("/api/orders", params=params)
    assert response.status_code == 200
    assert [order["id"] for order in response.json()] == expected_ids


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"offset": -1},
        {"limit": "not-an-integer"},
        {"offset": "not-an-integer"},
    ],
)
def test_api_order_pagination_rejects_invalid_parameters(client, params):
    response = client.get("/api/orders", params=params)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "请求参数格式不正确",
        }
    }
