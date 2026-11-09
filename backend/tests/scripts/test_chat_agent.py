import json
from datetime import date
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy.orm import Session

from shopcare.agent.graph import build_agent_graph
from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import Conversation
from shopcare.scripts import chat_agent
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.conversations import create_conversation


@pytest.fixture
def chat_cli_context(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'business.db').as_posix()}",
        llm_model_id="mock-chat",
    )
    engine = build_engine(settings.database_url)
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            seed_demo_data(session)
    finally:
        engine.dispose()
    client, embedding = Mock(), Mock()
    monkeypatch.setattr(chat_agent, "Settings", lambda: settings)
    monkeypatch.setattr(chat_agent, "build_chat_client", Mock(return_value=client))
    monkeypatch.setattr(chat_agent, "build_embedding_client", Mock(return_value=embedding))
    return settings, client, embedding


def test_chat_preview_does_not_open_database_or_create_clients(
    chat_cli_context, tmp_path, monkeypatch, capsys
):
    factory = Mock(side_effect=AssertionError("预览不能打开数据库"))
    monkeypatch.setattr(chat_agent, "build_engine", factory)
    checkpoint = tmp_path / "private" / "checkpoints.db"
    output = tmp_path / "preview.json"
    assert chat_agent.main(["--checkpoint-db", str(checkpoint), "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["action"] == "create"
    assert report["as_of"] == "2026-11-02"
    assert not checkpoint.exists()
    assert not output.exists()
    factory.assert_not_called()
    chat_agent.build_chat_client.assert_not_called()
    chat_agent.build_embedding_client.assert_not_called()


def test_chat_cli_creates_and_continues_same_conversation(chat_cli_context, tmp_path, capsys):
    settings, client, embedding = chat_cli_context
    checkpoint = tmp_path / "private" / "checkpoints.db"
    output = tmp_path / "reports" / "chat.json"
    payload = {
        "status": "needs_clarification",
        "answer": "请补充具体故障，目前仅提供咨询。",
        "citation_chunk_ids": [],
        "limitations": [],
        "policy_quotes": [],
    }
    client.bind_tools.return_value.invoke.return_value = AIMessage(content=json.dumps(payload))
    arguments = ["--run", "--checkpoint-db", str(checkpoint)]
    assert chat_agent.main([*arguments, "--output", str(output)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == first
    assert first["conversation_status"] == "ready"
    assert first["answer_validated"] is True
    assert first["answer"] == {
        key: value for key, value in payload.items() if key != "policy_quotes"
    }
    record_id = first["conversation_id"]

    # 用新的模型响应对象模拟另一轮服务请求。
    client.bind_tools.return_value.invoke.return_value = AIMessage(content=json.dumps(payload))
    assert (
        chat_agent.main([*arguments, "--conversation-id", record_id, "--query", "左边没声音"]) == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["conversation_id"] == record_id
    assert second["conversation_status"] == "ready"
    assert second["as_of"] == first["as_of"]
    received = client.bind_tools.return_value.invoke.call_args.args[0]
    assert [m.content for m in received if isinstance(m, HumanMessage)] == [
        "订单O004的O004-I01耳机坏了，能换货吗？",
        "左边没声音",
    ]
    embedding.embed_query.assert_not_called()
    engine = build_engine(settings.database_url)
    try:
        with Session(engine) as session:
            assert session.get(Conversation, record_id).status == "ready"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--query", " "],
        ["--query", "x" * 1001],
        ["--conversation-id", "existing", "--as-of", "2026-11-05"],
    ],
)
def test_chat_cli_rejects_invalid_input_before_creating_clients(
    chat_cli_context, capsys, arguments
):
    assert chat_agent.main(["--run", *arguments]) == 1
    assert capsys.readouterr().out == ""
    chat_agent.build_chat_client.assert_not_called()
    chat_agent.build_embedding_client.assert_not_called()


def test_chat_cli_failed_answer_reports_failure_and_cannot_continue(
    chat_cli_context, tmp_path, capsys
):
    _, client, _ = chat_cli_context
    client.bind_tools.return_value.invoke.return_value = AIMessage(content="invalid JSON")
    arguments = ["--run", "--checkpoint-db", str(tmp_path / "checkpoints.db")]
    assert chat_agent.main(arguments) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["answer"] is None
    assert first["answer_validated"] is False
    assert first["conversation_status"] == "failed"
    assert first["error_code"] == "CONVERSATION_RUN_FAILED"
    client.reset_mock()
    assert chat_agent.main([*arguments, "--conversation-id", first["conversation_id"]]) == 1
    second = json.loads(capsys.readouterr().out)
    assert second["conversation_status"] == "failed"
    client.bind_tools.assert_not_called()


def test_chat_cli_cannot_continue_another_users_conversation(chat_cli_context, tmp_path, capsys):
    settings, client, _ = chat_cli_context
    engine = build_engine(settings.database_url)
    try:
        with Session(engine) as session, session.begin():
            record_id = create_conversation(session, "U002", date(2026, 11, 2)).id
        assert (
            chat_agent.main(
                [
                    "--run",
                    "--checkpoint-db",
                    str(tmp_path / "checkpoints.db"),
                    "--conversation-id",
                    record_id,
                ]
            )
            == 1
        )
        assert capsys.readouterr().out == ""
        client.bind_tools.assert_not_called()
        with Session(engine) as session:
            assert session.get(Conversation, record_id).status == "ready"
    finally:
        engine.dispose()


def test_chat_cli_refuses_legacy_answer_before_starting_and_preserves_history(
    chat_cli_context, tmp_path, capsys
):
    settings, client, embedding = chat_cli_context
    engine = build_engine(settings.database_url)
    checkpoint = tmp_path / "legacy.db"
    legacy_answer = json.dumps(
        {
            "status": "needs_clarification",
            "answer": "请补充故障。",
            "citation_chunk_ids": [],
            "limitations": [],
        }
    )
    try:
        with Session(engine) as session, session.begin():
            conversation = create_conversation(session, "U001", date(2026, 11, 2))
            record_id, thread_id = conversation.id, conversation.thread_id
        config = {"configurable": {"thread_id": thread_id}}
        old_client = Mock()
        old_client.bind_tools.return_value.invoke.return_value = AIMessage(content=legacy_answer)
        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
            graph = build_agent_graph(old_client, [], checkpointer=saver)
            graph.invoke(
                {
                    "messages": [HumanMessage(content="耳机坏了")],
                    "model_calls": 0,
                    "tool_calls": 0,
                    "stop_reason": "running",
                },
                config=config,
            )
        assert (
            chat_agent.main(
                ["--run", "--conversation-id", record_id, "--checkpoint-db", str(checkpoint)]
            )
            == 1
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "旧会话缺少policy_quotes" in captured.err
        assert "请新建会话" in captured.err
        client.bind_tools.assert_not_called()
        embedding.embed_query.assert_not_called()
        with Session(engine) as session:
            assert session.get(Conversation, record_id).status == "ready"
        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
            state = saver.get_tuple(config).checkpoint["channel_values"]
            assert len(state["messages"]) == 2
            assert state["messages"][-1].content == legacy_answer
    finally:
        engine.dispose()


def test_chat_cli_rejects_missing_checkpoint_without_starting_a_fresh_history(
    chat_cli_context, tmp_path, capsys
):
    settings, client, _ = chat_cli_context
    client.bind_tools.return_value.invoke.return_value = AIMessage(
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
    assert chat_agent.main(["--run", "--checkpoint-db", str(tmp_path / "original.db")]) == 0
    first = json.loads(capsys.readouterr().out)
    client.reset_mock()
    assert (
        chat_agent.main(
            [
                "--run",
                "--checkpoint-db",
                str(tmp_path / "wrong-file.db"),
                "--conversation-id",
                first["conversation_id"],
                "--query",
                "左边没声音",
            ]
        )
        == 1
    )
    assert capsys.readouterr().out == ""
    client.bind_tools.assert_not_called()
    engine = build_engine(settings.database_url)
    try:
        with Session(engine) as session:
            assert session.get(Conversation, first["conversation_id"]).status == "ready"
    finally:
        engine.dispose()
