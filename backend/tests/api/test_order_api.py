import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopcare.api.dependencies import get_current_user_id
from shopcare.db import build_engine, init_db
from shopcare.main import app, settings
from shopcare.scripts.seed import seed_demo_data


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
