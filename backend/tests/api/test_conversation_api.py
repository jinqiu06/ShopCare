from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api import conversations as conversations_api
from shopcare.api.dependencies import get_current_user_id
from shopcare.db import build_engine, init_db
from shopcare.db.models import Conversation
from shopcare.main import app, settings
from shopcare.scripts.seed import seed_demo_data


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'conversation-api.db').as_posix()}"
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


def test_create_conversation_commits_and_returns_only_public_fields(client):
    first = client.post(
        "/api/conversations?user_id=U002",
        headers={"X-User-ID": "U002"},
        json={"as_of": "2026-11-02"},
    )
    assert first.status_code == 201
    payload = first.json()
    assert UUID(payload["id"]).version == 4
    assert payload == {"id": payload["id"], "status": "ready", "as_of": "2026-11-02"}
    second = client.post("/api/conversations", json={"as_of": "2026-11-02"})
    assert second.status_code == 201
    assert second.json()["id"] != payload["id"]
    # 独立连接检查事务已提交，身份来自服务端而非header/query。
    with Session(app.state.engine) as session:
        first_record = session.get(Conversation, payload["id"])
        second_record = session.get(Conversation, second.json()["id"])
        assert first_record.user_id == second_record.user_id == "U001"
        assert first_record.thread_id != first_record.id
        assert first_record.thread_id != second_record.thread_id
        assert str(first_record.as_of) == "2026-11-02"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"as_of": "invalid-date"},
        {"as_of": None},
        {"as_of": "2026-11-02", "user_id": "U002"},
        {"as_of": "2026-11-02", "thread_id": "client-thread"},
        {"as_of": "2026-11-02", "status": "running"},
    ],
)
def test_create_conversation_rejects_invalid_body_before_service(client, monkeypatch, body):
    create = Mock(side_effect=AssertionError("invalid request must not call service"))
    monkeypatch.setattr(conversations_api, "create_conversation", create)
    response = client.post("/api/conversations", json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": "请求参数格式不正确"}
    }
    create.assert_not_called()
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Conversation)) == 0


def test_create_conversation_missing_trusted_user_returns_safe_404(client):
    app.dependency_overrides[get_current_user_id] = lambda: "missing-user"
    response = client.post("/api/conversations", json={"as_of": "2026-11-02"})
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "USER_NOT_FOUND", "message": "当前用户不存在"}}
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Conversation)) == 0


@pytest.mark.parametrize("owner", ["U001", "U002"])
@pytest.mark.parametrize("status", ["ready", "running", "failed"])
def test_read_conversation_returns_owned_record_without_changing_it(client, owner, status):
    app.dependency_overrides[get_current_user_id] = lambda: owner
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    with Session(app.state.engine) as session, session.begin():
        record = session.get(Conversation, created["id"])
        record.status = status
        original_thread = record.thread_id
        original_updated_at = record.updated_at
    response = client.get(
        f"/api/conversations/{created['id']}?user_id=other-user",
        headers={"X-User-ID": "other-user"},
    )
    assert response.status_code == 200
    assert response.json() == {**created, "status": status}
    with Session(app.state.engine) as session:
        record = session.get(Conversation, created["id"])
        assert record.user_id == owner
        assert record.status == status
        assert record.thread_id == original_thread
        assert record.updated_at == original_updated_at


@pytest.mark.parametrize("requester", ["U001", "unknown-user"])
def test_read_conversation_missing_and_unowned_return_identical_404(client, requester):
    app.dependency_overrides[get_current_user_id] = lambda: "U002"
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    app.dependency_overrides[get_current_user_id] = lambda: requester
    unowned = client.get(
        f"/api/conversations/{created['id']}?user_id=U002",
        headers={"X-User-ID": "U002"},
    )
    missing = client.get("/api/conversations/missing-conversation")
    assert unowned.status_code == missing.status_code == 404
    assert (
        unowned.json()
        == missing.json()
        == {"error": {"code": "CONVERSATION_NOT_FOUND", "message": "会话不存在或不可访问"}}
    )
    with Session(app.state.engine) as session:
        assert session.get(Conversation, created["id"]).status == "ready"


def test_read_conversation_database_failure_returns_safe_503(client, monkeypatch):
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    query = Mock(side_effect=SQLAlchemyError("private SQL and credentials"))
    monkeypatch.setattr(conversations_api, "get_conversation_for_user", query)
    response = client.get(f"/api/conversations/{created['id']}")
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "CONVERSATION_UNAVAILABLE",
            "message": "会话服务暂不可用，请稍后重试",
        }
    }
    assert query.call_args.args[1:] == ("U001", created["id"])
    with Session(app.state.engine) as session:
        assert session.get(Conversation, created["id"]).status == "ready"


@pytest.mark.parametrize("stage", ["after_flush", "commit"])
def test_create_conversation_database_failure_rolls_back_and_does_not_leak_details(
    client, monkeypatch, stage
):
    original_create = conversations_api.create_conversation

    def fail_after_flush(*args):
        original_create(*args)
        raise SQLAlchemyError("private SQL and database connection detail")

    def fail_commit(session):
        raise SQLAlchemyError("private commit detail")

    if stage == "after_flush":
        monkeypatch.setattr(conversations_api, "create_conversation", fail_after_flush)
    else:
        event.listen(Session, "before_commit", fail_commit)
    try:
        response = client.post("/api/conversations", json={"as_of": "2026-11-02"})
    finally:
        if stage == "commit":
            event.remove(Session, "before_commit", fail_commit)
        monkeypatch.setattr(conversations_api, "create_conversation", original_create)
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "CONVERSATION_UNAVAILABLE",
            "message": "会话服务暂不可用，请稍后重试",
        }
    }
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Conversation)) == 0
    # 故障后新请求仍能正常使用数据库，失败请求没有留下半条会话。
    assert client.post("/api/conversations", json={"as_of": "2026-11-02"}).status_code == 201
