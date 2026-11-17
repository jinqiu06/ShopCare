from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api import after_sales as after_sales_api
from shopcare.api.dependencies import get_current_user_id
from shopcare.db import build_engine, init_db
from shopcare.db.models import AfterSalesDraft, Order
from shopcare.main import app, settings
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.policies.chunker import split_policy
from shopcare.services.policies.loader import load_policies
from shopcare.services.policies.store import replace_policy_chunks


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'draft-api.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = build_engine(url)
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            seed_demo_data(session)
    finally:
        engine.dispose()
    previous_overrides = app.dependency_overrides.copy()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.fixture
def payload():
    return {
        "order_id": "O004",
        "item_id": "O004-I01",
        "request_type": "exchange",
        "reason": "无法充电",
        "as_of": "2026-11-02",
    }


@pytest.fixture
def submission_policy(client):
    with Session(app.state.engine) as session, session.begin():
        for policy in load_policies(Path(__file__).resolve().parents[3] / "data/policies"):
            replace_policy_chunks(session, policy, split_policy(policy))


def test_confirm_http_lifecycle_replay_and_receipt_isolation(client, payload, submission_policy):
    draft = client.post("/api/after-sales/drafts", json=payload).json()
    path = f"/api/after-sales/drafts/{draft['id']}"
    confirmation = {
        "expected_version": 1,
        "confirmed": True,
        "facts": {"issue_type": "malfunction"},
    }
    assert client.get(path + "/application").status_code == 404
    first = client.post(path + "/confirm", json=confirmation)
    assert first.status_code == 200, first.text
    application = first.json()
    assert application["status"] == "pending_review"
    assert application["draft_id"] == draft["id"]
    assert application["confirmed_version"] == 1
    assert application["snapshot"]["policy_id"] == "digital-exchange"
    assert "user_id" not in application
    assert client.post(path + "/confirm", json=confirmation).json() == application
    assert client.get(path + "/application").json() == application
    current = client.get(path).json()
    assert current["status"] == "submitted" and current["version"] == 2
    assert client.post(path + "/cancel", json={"expected_version": 2}).status_code == 409
    assert (
        client.post(path + "/confirm", json={**confirmation, "expected_version": 2}).status_code
        == 409
    )
    app.dependency_overrides[get_current_user_id] = lambda: "U002"
    assert client.get(path + "/application").status_code == 404
    assert client.post(path + "/confirm", json=confirmation).status_code == 404


@pytest.mark.parametrize(
    "changes,expected_status,code",
    [
        ({"confirmed": False}, 409, "DRAFT_CONFLICT"),
        ({"confirmed": "true"}, 422, "VALIDATION_ERROR"),
        ({"confirmed": 1}, 422, "VALIDATION_ERROR"),
        ({"expected_version": True}, 422, "VALIDATION_ERROR"),
        ({"facts": {}}, 409, "SUBMISSION_NOT_ELIGIBLE"),
        ({"facts": {"unused": "true"}}, 422, "VALIDATION_ERROR"),
        ({"as_of": "2026-11-02"}, 422, "VALIDATION_ERROR"),
    ],
)
def test_confirm_http_requires_explicit_typed_confirmation(
    client,
    payload,
    submission_policy,
    changes,
    expected_status,
    code,
):
    draft = client.post("/api/after-sales/drafts", json=payload).json()
    response = client.post(
        f"/api/after-sales/drafts/{draft['id']}/confirm",
        json={
            "expected_version": 1,
            "confirmed": True,
            "facts": {"issue_type": "malfunction"},
            **changes,
        },
    )
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == code
    assert client.get(f"/api/after-sales/drafts/{draft['id']}").json()["version"] == 1


def test_confirmation_commit_failure_returns_no_receipt_and_rolls_back(
    client,
    payload,
    submission_policy,
):
    draft = client.post("/api/after-sales/drafts", json=payload).json()
    failing_session = Session(app.state.engine)

    def reject_commit(session):
        raise SQLAlchemyError("private commit failure")

    def session_override():
        with failing_session:
            yield failing_session

    event.listen(failing_session, "before_commit", reject_commit)
    app.dependency_overrides[after_sales_api.get_session] = session_override
    response = client.post(
        f"/api/after-sales/drafts/{draft['id']}/confirm",
        json={
            "expected_version": 1,
            "confirmed": True,
            "facts": {"issue_type": "malfunction"},
        },
    )
    app.dependency_overrides.pop(after_sales_api.get_session)
    assert response.status_code == 503
    assert "private" not in response.text
    assert client.get(f"/api/after-sales/drafts/{draft['id']}/application").status_code == 404
    assert client.get(f"/api/after-sales/drafts/{draft['id']}").json()["version"] == 1


@pytest.mark.parametrize(
    "owner,order_id,item_id",
    [
        ("U001", "O004", "O004-I01"),
        ("U002", "O011", "O011-I01"),
    ],
)
@pytest.mark.parametrize("request_type", ["return", "exchange"])
def test_create_draft_http_commits_and_returns_public_fields(
    client, payload, owner, order_id, item_id, request_type
):
    app.dependency_overrides[get_current_user_id] = lambda: owner
    with Session(app.state.engine) as session:
        original_status = session.get(Order, order_id).status
    response = client.post(
        "/api/after-sales/drafts?user_id=someone-else",
        headers={"X-User-ID": "someone-else"},
        json={
            **payload,
            "order_id": f" {order_id} ",
            "item_id": item_id,
            "request_type": request_type,
            "reason": "  无法充电  ",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert set(data) == {
        "id",
        "order_id",
        "item_id",
        "request_type",
        "reason",
        "status",
        "version",
        "as_of",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert UUID(data["id"]).version == 4
    assert (data["order_id"], data["item_id"], data["request_type"]) == (
        order_id,
        item_id,
        request_type,
    )
    assert (data["status"], data["version"], data["reason"]) == ("pending", 1, "无法充电")
    assert data["as_of"] == "2026-11-02"
    assert data["created_at"] == data["updated_at"]
    assert datetime.fromisoformat(data["expires_at"]) - datetime.fromisoformat(
        data["created_at"]
    ) == timedelta(hours=24)
    with Session(app.state.engine) as session:
        saved = session.get(AfterSalesDraft, data["id"])
        assert (saved.user_id, saved.order_id, saved.item_id) == (owner, order_id, item_id)
        assert session.get(Order, order_id).status == original_status


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": " \t\n "},
        {"reason": "x" * 1001},
        {"order_id": "x" * 51},
        {"request_type": "refund"},
        {"as_of": "bad-date"},
        {"reason": None},
        {"user_id": "U002"},
        {"status": "cancelled"},
        {"version": 9},
        {"expires_at": "2027-01-01"},
    ],
)
def test_create_draft_http_rejects_invalid_body_before_service(
    client, payload, monkeypatch, changes
):
    create = Mock(side_effect=AssertionError("invalid payload reached service"))
    monkeypatch.setattr(after_sales_api, "create_after_sales_draft", create)
    response = client.post("/api/after-sales/drafts", json={**payload, **changes})
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": "请求参数格式不正确"}
    }
    create.assert_not_called()


@pytest.mark.parametrize(
    "order_id,item_id",
    [
        ("missing-order", "missing-item"),
        ("O011", "O011-I01"),
        ("O004", "O001-I01"),
    ],
)
def test_create_draft_http_hides_missing_and_unowned_context(client, payload, order_id, item_id):
    response = client.post(
        "/api/after-sales/drafts?user_id=U002",
        headers={"X-User-ID": "U002"},
        json={**payload, "order_id": order_id, "item_id": item_id},
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "ORDER_CONTEXT_NOT_FOUND",
            "message": "订单或商品不存在或不可访问",
        }
    }
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesDraft)) == 0


@pytest.mark.parametrize("stage", ["after_flush", "commit"])
def test_create_draft_http_database_failure_rolls_back(client, payload, monkeypatch, stage):
    original_create = after_sales_api.create_after_sales_draft

    def fail_after_flush(*args):
        original_create(*args)
        raise SQLAlchemyError("private SQL and credentials")

    def fail_commit(session):
        raise SQLAlchemyError("private commit detail")

    if stage == "after_flush":
        monkeypatch.setattr(after_sales_api, "create_after_sales_draft", fail_after_flush)
    else:
        event.listen(Session, "before_commit", fail_commit)
    try:
        response = client.post("/api/after-sales/drafts", json=payload)
    finally:
        if stage == "commit":
            event.remove(Session, "before_commit", fail_commit)
        monkeypatch.setattr(after_sales_api, "create_after_sales_draft", original_create)
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "DRAFT_UNAVAILABLE",
            "message": "草稿服务暂不可用，请稍后重试",
        }
    }
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesDraft)) == 0
    assert client.post("/api/after-sales/drafts", json=payload).status_code == 201


@pytest.mark.parametrize(
    "owner,order_id,item_id",
    [
        ("U001", "O004", "O004-I01"),
        ("U002", "O011", "O011-I01"),
    ],
)
@pytest.mark.parametrize("status", ["pending", "cancelled"])
def test_read_draft_http_returns_owned_record_without_mutation(
    client, payload, owner, order_id, item_id, status
):
    app.dependency_overrides[get_current_user_id] = lambda: owner
    created = client.post(
        "/api/after-sales/drafts", json={**payload, "order_id": order_id, "item_id": item_id}
    ).json()
    version = 1 if status == "pending" else 2
    with Session(app.state.engine) as session, session.begin():
        saved = session.get(AfterSalesDraft, created["id"])
        saved.status, saved.version = status, version
        saved.created_at -= timedelta(days=2)
        saved.expires_at = saved.created_at + timedelta(hours=24)
        before = (saved.status, saved.version, saved.created_at, saved.updated_at, saved.expires_at)
    response = client.get(
        f"/api/after-sales/drafts/{created['id']}?user_id=someone-else",
        headers={"X-User-ID": "someone-else"},
    )
    assert response.status_code == 200
    data = response.json()
    assert set(data) == set(created)
    assert (data["status"], data["version"]) == (status, version)
    assert (data["id"], data["order_id"], data["item_id"]) == (created["id"], order_id, item_id)
    with Session(app.state.engine) as session:
        saved = session.get(AfterSalesDraft, created["id"])
        assert saved.user_id == owner
        assert (
            saved.status,
            saved.version,
            saved.created_at,
            saved.updated_at,
            saved.expires_at,
        ) == before


@pytest.mark.parametrize("requester", ["U001", "missing-user"])
def test_read_draft_http_missing_and_unowned_return_same_404(client, payload, requester):
    app.dependency_overrides[get_current_user_id] = lambda: "U002"
    created = client.post(
        "/api/after-sales/drafts", json={**payload, "order_id": "O011", "item_id": "O011-I01"}
    ).json()
    app.dependency_overrides[get_current_user_id] = lambda: requester
    unowned = client.get(
        f"/api/after-sales/drafts/{created['id']}?user_id=U002",
        headers={"X-User-ID": "U002"},
    )
    missing = client.get("/api/after-sales/drafts/missing-draft")
    assert unowned.status_code == missing.status_code == 404
    assert (
        unowned.json()
        == missing.json()
        == {
            "error": {
                "code": "DRAFT_NOT_FOUND",
                "message": "草稿不存在或不可访问",
            }
        }
    )


@pytest.mark.parametrize("failure", ["database", "unexpected"])
def test_read_draft_http_maps_only_database_errors(client, monkeypatch, failure):
    error = (
        SQLAlchemyError("private SQL")
        if failure == "database"
        else RuntimeError("programming failure")
    )
    query = Mock(side_effect=error)
    monkeypatch.setattr(after_sales_api, "get_after_sales_draft_for_user", query)
    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="programming failure"):
            client.get("/api/after-sales/drafts/any-draft")
    else:
        response = client.get("/api/after-sales/drafts/any-draft")
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "DRAFT_UNAVAILABLE",
                "message": "草稿服务暂不可用，请稍后重试",
            }
        }


@pytest.mark.parametrize(
    "owner,order_id,item_id",
    [
        ("U001", "O004", "O004-I01"),
        ("U002", "O011", "O011-I01"),
    ],
)
@pytest.mark.parametrize("expired", [False, True])
def test_draft_http_create_read_cancel_round_trip(
    client, payload, owner, order_id, item_id, expired
):
    app.dependency_overrides[get_current_user_id] = lambda: owner
    created = client.post(
        "/api/after-sales/drafts", json={**payload, "order_id": order_id, "item_id": item_id}
    ).json()
    path = f"/api/after-sales/drafts/{created['id']}"
    assert client.get(path).json() == created
    if expired:
        with Session(app.state.engine) as session, session.begin():
            draft = session.get(AfterSalesDraft, created["id"])
            draft.created_at -= timedelta(days=2)
            draft.expires_at = draft.created_at + timedelta(hours=24)
        created = client.get(path).json()
    with Session(app.state.engine) as session:
        order_status = session.get(Order, order_id).status
    response = client.post(
        f"{path}/cancel?user_id=someone-else",
        headers={"X-User-ID": "someone-else"},
        json={"expected_version": 1},
    )
    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert set(cancelled) == set(created)
    assert (cancelled["status"], cancelled["version"]) == ("cancelled", 2)
    assert datetime.fromisoformat(cancelled["updated_at"]) >= datetime.fromisoformat(
        created["updated_at"]
    )
    for field in set(created) - {"status", "version", "updated_at"}:
        assert cancelled[field] == created[field]
    assert client.get(path).json() == cancelled
    with Session(app.state.engine) as session:
        draft = session.get(AfterSalesDraft, created["id"])
        assert (draft.user_id, draft.status, draft.version) == (owner, "cancelled", 2)
        assert session.get(Order, order_id).status == order_status


@pytest.mark.parametrize("already_cancelled,expected_version", [(False, 2), (True, 1), (True, 2)])
def test_cancel_draft_http_rejects_state_and_version_conflicts(
    client, payload, already_cancelled, expected_version
):
    created = client.post("/api/after-sales/drafts", json=payload).json()
    path = f"/api/after-sales/drafts/{created['id']}"
    if already_cancelled:
        assert client.post(f"{path}/cancel", json={"expected_version": 1}).status_code == 200
    before = client.get(path).json()
    response = client.post(f"{path}/cancel", json={"expected_version": expected_version})
    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "DRAFT_CONFLICT",
            "message": "草稿状态或版本已变化，请重新查询",
        }
    }
    assert client.get(path).json() == before


@pytest.mark.parametrize("requester", ["U001", "missing-user"])
def test_cancel_draft_http_missing_and_unowned_return_same_404(client, payload, requester):
    app.dependency_overrides[get_current_user_id] = lambda: "U002"
    created = client.post(
        "/api/after-sales/drafts", json={**payload, "order_id": "O011", "item_id": "O011-I01"}
    ).json()
    app.dependency_overrides[get_current_user_id] = lambda: requester
    unowned = client.post(
        f"/api/after-sales/drafts/{created['id']}/cancel?user_id=U002",
        headers={"X-User-ID": "U002"},
        json={"expected_version": 1},
    )
    missing = client.post(
        "/api/after-sales/drafts/missing-draft/cancel", json={"expected_version": 1}
    )
    assert unowned.status_code == missing.status_code == 404
    assert (
        unowned.json()
        == missing.json()
        == {
            "error": {
                "code": "DRAFT_NOT_FOUND",
                "message": "草稿不存在或不可访问",
            }
        }
    )
    with Session(app.state.engine) as session:
        draft = session.get(AfterSalesDraft, created["id"])
        assert (draft.status, draft.version) == ("pending", 1)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expected_version": 0},
        {"expected_version": -1},
        {"expected_version": "1"},
        {"expected_version": True},
        {"expected_version": 1.0},
        {"expected_version": None},
        {"expected_version": 1, "user_id": "U002"},
        {"expected_version": 1, "status": "cancelled"},
    ],
)
def test_cancel_draft_http_rejects_invalid_body_before_service(client, monkeypatch, body):
    cancel = Mock(side_effect=AssertionError("invalid payload reached cancellation"))
    monkeypatch.setattr(after_sales_api, "cancel_after_sales_draft", cancel)
    response = client.post("/api/after-sales/drafts/unused/cancel", json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "请求参数格式不正确",
        }
    }
    cancel.assert_not_called()


@pytest.mark.parametrize("failure", ["after_update", "commit", "unexpected"])
def test_cancel_draft_http_rolls_back_failures_and_maps_only_database_errors(
    client, payload, monkeypatch, failure
):
    created = client.post("/api/after-sales/drafts", json=payload).json()
    path = f"/api/after-sales/drafts/{created['id']}"
    original_cancel = after_sales_api.cancel_after_sales_draft

    def fail_after_update(*args):
        original_cancel(*args)
        if failure == "unexpected":
            raise RuntimeError("private programming failure")
        raise SQLAlchemyError("private update SQL and credentials")

    def fail_commit(session):
        raise SQLAlchemyError("private commit detail")

    if failure == "commit":
        event.listen(Session, "before_commit", fail_commit)
    else:
        monkeypatch.setattr(after_sales_api, "cancel_after_sales_draft", fail_after_update)
    try:
        if failure == "unexpected":
            with pytest.raises(RuntimeError, match="private programming failure"):
                client.post(f"{path}/cancel", json={"expected_version": 1})
        else:
            response = client.post(f"{path}/cancel", json={"expected_version": 1})
            assert response.status_code == 503
            assert response.json() == {
                "error": {
                    "code": "DRAFT_UNAVAILABLE",
                    "message": "草稿服务暂不可用，请稍后重试",
                }
            }
    finally:
        if failure == "commit":
            event.remove(Session, "before_commit", fail_commit)
        monkeypatch.setattr(after_sales_api, "cancel_after_sales_draft", original_cancel)
    assert client.get(path).json() == created
    assert client.post(f"{path}/cancel", json={"expected_version": 1}).status_code == 200
