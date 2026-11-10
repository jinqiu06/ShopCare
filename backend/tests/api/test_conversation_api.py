import json
import sqlite3
from datetime import date
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import APITimeoutError, OpenAIError
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopcare.api import conversations as conversations_api
from shopcare.api.dependencies import get_current_user_id, open_agent_checkpointer
from shopcare.db import build_engine, init_db
from shopcare.db.models import Conversation, PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.main import app, settings
from shopcare.schemas.answers import PolicyAnswer
from shopcare.scripts.seed import seed_demo_data
from shopcare.services import conversation_messages as message_service
from shopcare.services.conversations import ConversationTurnConflictError


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'conversation-api.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(
        settings, "agent_checkpoint_db", str(tmp_path / "private" / "checkpoints.db")
    )
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
        first_thread, second_thread = first_record.thread_id, second_record.thread_id
    with open_agent_checkpointer(app.state.engine, settings.agent_checkpoint_db) as saver:
        for thread_id in (first_thread, second_thread):
            saved = saver.get_tuple({"configurable": {"thread_id": thread_id}})
            assert saved is not None
            assert saved.checkpoint["channel_values"] == {}
            assert saved.metadata["shopcare_initialized"] is True
            assert saved.metadata["step"] == -1


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


@pytest.mark.parametrize("failure", ["open", "initialize", "same_file"])
def test_create_conversation_checkpoint_failure_rolls_back_business_record(
    client, monkeypatch, failure
):
    with monkeypatch.context() as patch:
        if failure == "open":
            patch.setattr(
                conversations_api,
                "open_agent_checkpointer",
                Mock(side_effect=PermissionError("private checkpoint path detail")),
            )
        elif failure == "initialize":
            patch.setattr(
                conversations_api,
                "initialize_agent_checkpoint",
                Mock(side_effect=sqlite3.OperationalError("private checkpoint SQL detail")),
            )
        else:
            patch.setattr(settings, "agent_checkpoint_db", app.state.engine.url.database)
        response = client.post("/api/conversations", json={"as_of": "2026-11-02"})
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "CONVERSATION_UNAVAILABLE",
            "message": "会话服务暂不可用，请稍后重试",
        }
    }
    with Session(app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Conversation)) == 0
    assert client.post("/api/conversations", json={"as_of": "2026-11-02"}).status_code == 201


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "sdk_failure",
        "run_failure",
        "api_timeout",
        "timeout",
        "upstream",
        "conflict",
        "execution_file",
        "execution_os",
    ],
)
def test_message_http_closes_request_resources(client, monkeypatch, caplog, outcome):
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    answer = PolicyAnswer(
        status="needs_clarification", answer="请补充故障。", citation_chunk_ids=[], limitations=[]
    )
    chat_factory = Mock(return_value=object())
    embedding_factory = Mock(return_value=object())
    run = Mock(return_value=answer)
    if outcome == "sdk_failure":
        embedding_factory.side_effect = ValueError("private credential detail")
    elif outcome == "run_failure":
        run.side_effect = RuntimeError("simulated execution failure")
    elif outcome == "conflict":
        run.side_effect = ConversationTurnConflictError("private conflict detail")
    elif outcome in {"execution_file", "execution_os"}:
        run.side_effect = (
            FileNotFoundError("execution file missing")
            if outcome == "execution_file"
            else OSError("execution OS failure")
        )
    elif outcome in {"api_timeout", "timeout", "upstream"}:
        errors = {
            "api_timeout": APITimeoutError(
                request=httpx.Request("POST", "https://example.invalid")
            ),
            "timeout": TimeoutError("private timeout detail"),
            "upstream": OpenAIError("private upstream detail"),
        }
        failing_model = Mock()
        failing_model.bind_tools.return_value.invoke.side_effect = errors[outcome]
        chat_factory.return_value = failing_model
        run = Mock(wraps=message_service.run_conversation_turn)
    monkeypatch.setattr(message_service, "build_chat_client", chat_factory)
    monkeypatch.setattr(message_service, "build_embedding_client", embedding_factory)
    monkeypatch.setattr(message_service, "run_conversation_turn", run)
    path = f"/api/conversations/{created['id']}/messages"
    if outcome in {"run_failure", "execution_file", "execution_os"}:
        error_type = RuntimeError if outcome == "run_failure" else OSError
        with pytest.raises(error_type):
            client.post(path, json={"query": "左边没声音"})
    else:
        response = client.post(path, json={"query": "左边没声音"})
        if outcome == "success":
            assert response.status_code == 200
            assert response.json() == answer.model_dump()
        else:
            expected = {
                "sdk_failure": (503, "AGENT_NOT_CONFIGURED", "对话服务配置暂不可用"),
                "api_timeout": (504, "AGENT_TIMEOUT", "对话服务超时，请稍后查询会话状态"),
                "timeout": (504, "AGENT_TIMEOUT", "对话服务超时，请稍后查询会话状态"),
                "upstream": (502, "AGENT_UPSTREAM_ERROR", "对话服务暂不可用，请稍后查询会话状态"),
                "conflict": (409, "CONVERSATION_NOT_READY", "会话当前不可执行"),
            }[outcome]
            assert response.status_code == expected[0]
            assert response.json() == {"error": {"code": expected[1], "message": expected[2]}}
            if outcome == "sdk_failure":
                run.assert_not_called()
            elif outcome in {"api_timeout", "timeout", "upstream"}:
                with Session(app.state.engine) as session:
                    assert session.get(Conversation, created["id"]).status == "failed"
    http_client = chat_factory.call_args.kwargs["http_client"]
    assert embedding_factory.call_args.kwargs["http_client"] is http_client
    assert http_client.is_closed
    if outcome != "sdk_failure":
        arguments = run.call_args.args
        assert arguments[0] is app.state.engine
        assert arguments[1] is chat_factory.return_value
        assert arguments[2] is embedding_factory.return_value
        assert arguments[4:] == ("U001", created["id"], "左边没声音")
        with pytest.raises(sqlite3.ProgrammingError):
            arguments[3].conn.execute("SELECT 1")
    assert "post" in app.openapi()["paths"]["/api/conversations/{conversation_id}/messages"]
    if outcome == "upstream":
        assert "type=OpenAIError status=None cause=None" in caplog.text
        assert "private upstream detail" not in caplog.text


@pytest.mark.parametrize("failure", ["missing_file", "missing_thread", "permission", "sqlite"])
def test_message_function_does_not_build_clients_when_checkpoint_file_missing(
    client, monkeypatch, tmp_path, failure
):
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    missing = tmp_path / "missing-checkpoints.db"
    if failure == "missing_file":
        monkeypatch.setattr(settings, "agent_checkpoint_db", str(missing))
    elif failure == "missing_thread":
        with Session(app.state.engine) as session, session.begin():
            session.get(Conversation, created["id"]).thread_id = "missing-thread"
    else:
        error = (
            PermissionError("private checkpoint path")
            if failure == "permission"
            else sqlite3.OperationalError("private checkpoint SQL")
        )
        monkeypatch.setattr(message_service, "open_agent_checkpointer", Mock(side_effect=error))
    chat_factory, embedding_factory, run = Mock(), Mock(), Mock()
    monkeypatch.setattr(message_service, "build_chat_client", chat_factory)
    monkeypatch.setattr(message_service, "build_embedding_client", embedding_factory)
    monkeypatch.setattr(message_service, "run_conversation_turn", run)
    response = client.post(f"/api/conversations/{created['id']}/messages", json={"query": "故障"})
    if failure in {"missing_file", "missing_thread"}:
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "CONVERSATION_CHECKPOINT_MISSING",
            "message": "会话历史不存在，无法继续，请新建会话",
        }
    else:
        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "CONVERSATION_UNAVAILABLE",
            "message": "会话服务暂不可用，请稍后重试",
        }
    chat_factory.assert_not_called()
    embedding_factory.assert_not_called()
    run.assert_not_called()
    assert not missing.exists()
    with Session(app.state.engine) as session:
        assert session.get(Conversation, created["id"]).status == "ready"


@pytest.mark.parametrize("case", ["database", "missing", "unowned", "running", "failed"])
def test_message_precheck_rejects_before_opening_resources(client, monkeypatch, case):
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    with Session(app.state.engine) as session, session.begin():
        record = session.get(Conversation, created["id"])
        if case == "unowned":
            record.user_id = "U002"
        elif case in {"running", "failed"}:
            record.status = case
        original_status = record.status
        original_updated_at = record.updated_at
    if case == "database":
        monkeypatch.setattr(
            message_service,
            "get_conversation_for_user",
            Mock(side_effect=SQLAlchemyError("private SQL and credentials")),
        )
    resources = [Mock(), Mock(), Mock(), Mock()]
    for name, mock in zip(
        (
            "open_agent_checkpointer",
            "build_chat_client",
            "build_embedding_client",
            "run_conversation_turn",
        ),
        resources,
        strict=True,
    ):
        monkeypatch.setattr(message_service, name, mock)
    target = "missing-conversation" if case == "missing" else created["id"]
    response = client.post(
        f"/api/conversations/{target}/messages?user_id=U002",
        headers={"X-User-ID": "U002"},
        json={"query": "故障"},
    )
    if case == "database":
        expected = (503, "CONVERSATION_UNAVAILABLE", "会话服务暂不可用，请稍后重试")
    elif case in {"missing", "unowned"}:
        expected = (404, "CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
    else:
        expected = (409, "CONVERSATION_NOT_READY", "会话当前不可执行")
    assert response.status_code == expected[0]
    assert response.json()["error"] == {"code": expected[1], "message": expected[2]}
    for mock in resources:
        mock.assert_not_called()
    with Session(app.state.engine) as session:
        record = session.get(Conversation, created["id"])
        assert record.status == original_status
        assert record.updated_at == original_updated_at


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": " "},
        {"query": "x" * 1001},
        {"query": None},
        {"query": "故障", "user_id": "U002"},
        {"query": "故障", "as_of": "2026-11-10"},
    ],
)
def test_message_http_rejects_invalid_body_before_service(client, monkeypatch, body):
    send = Mock(side_effect=AssertionError("invalid request must not enter service"))
    monkeypatch.setattr(conversations_api, "send_message", send)
    response = client.post("/api/conversations/unused/messages", json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "VALIDATION_ERROR", "message": "请求参数格式不正确"}
    }
    send.assert_not_called()


def test_message_http_two_turns_persist_history_and_retrieve_current_evidence(client, monkeypatch):
    with Session(app.state.engine) as session, session.begin():
        session.add(
            PolicyChunkRecord(
                chunk_id="digital:v1:000",
                policy_id="digital",
                version="v1",
                title="模拟换货政策",
                category="数码",
                effective_from=date(2026, 1, 1),
                text="模拟政策",
                start_char=0,
                end_char=4,
            )
        )
        session.flush()
        session.add(
            PolicyEmbeddingRecord(
                chunk_id="digital:v1:000",
                model_id="test-model",
                dimensions=2,
                vector=[1.0, 0.0],
            )
        )
    embedding = Mock()
    embedding.model, embedding.dimensions = "test-model", 2
    embedding.embed_query.return_value = [1.0, 0.0]
    first_model, second_model = Mock(), Mock()
    first_model.bind_tools.return_value.invoke.return_value = AIMessage(
        content=json.dumps(
            {
                "status": "needs_clarification",
                "answer": "请补充具体故障。",
                "citation_chunk_ids": [],
                "limitations": [],
                "policy_quotes": [],
            }
        )
    )
    second_model.bind_tools.return_value.invoke.side_effect = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "policy-second-turn",
                    "name": "search_order_policies",
                    "args": {"order_id": "O004", "item_id": "O004-I01", "query": "耳机换货"},
                }
            ],
        ),
        AIMessage(
            content=json.dumps(
                {
                    "status": "answered",
                    "answer": "这是模拟政策咨询，尚未创建任何申请。",
                    "citation_chunk_ids": ["digital:v1:000"],
                    "limitations": [],
                    "policy_quotes": [{"chunk_id": "digital:v1:000", "quote": "模拟政策"}],
                }
            )
        ),
    ]
    chat_factory = Mock(side_effect=[first_model, second_model])
    embedding_factory = Mock(return_value=embedding)
    monkeypatch.setattr(message_service, "build_chat_client", chat_factory)
    monkeypatch.setattr(message_service, "build_embedding_client", embedding_factory)
    created = client.post("/api/conversations", json={"as_of": "2026-11-02"}).json()
    path = f"/api/conversations/{created['id']}/messages"
    first = client.post(path, json={"query": "订单O004的O004-I01耳机坏了"})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "needs_clarification"
    assert first.json()["citation_chunk_ids"] == []
    embedding.embed_query.assert_not_called()
    assert client.get(f"/api/conversations/{created['id']}").json() == created

    second = client.post(path, json={"query": "  左边没声音  "})
    assert second.status_code == 200, second.text
    assert set(second.json()) == {"status", "answer", "citation_chunk_ids", "limitations"}
    assert second.json()["citation_chunk_ids"] == ["digital:v1:000"]
    assert second.json()["status"] == "answered"
    embedding.embed_query.assert_called_once_with("耳机换货")
    second_input = second_model.bind_tools.return_value.invoke.call_args_list[0].args[0]
    assert [m.content for m in second_input if isinstance(m, HumanMessage)] == [
        "订单O004的O004-I01耳机坏了",
        "左边没声音",
    ]
    assert client.get(f"/api/conversations/{created['id']}").json() == created
    with Session(app.state.engine) as session:
        thread_id = session.get(Conversation, created["id"]).thread_id
    with open_agent_checkpointer(app.state.engine, settings.agent_checkpoint_db) as saver:
        saved = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        assert saved.checkpoint["channel_values"]["model_calls"] == 2
        assert any(
            isinstance(m, ToolMessage) for m in saved.checkpoint["channel_values"]["messages"]
        )
    for call in chat_factory.call_args_list:
        assert call.kwargs["http_client"].is_closed
    assert (
        chat_factory.call_args_list[0].kwargs["http_client"]
        is not (chat_factory.call_args_list[1].kwargs["http_client"])
    )


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
