import json
from datetime import date
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import text
from sqlalchemy.orm import Session

from shopcare.core.config import Settings
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.scripts import run_agent
from shopcare.scripts.seed import seed_demo_data


@pytest.fixture
def agent_cli_context(tmp_path, monkeypatch):
    database = tmp_path / "demo.db"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{database.as_posix()}",
        llm_model_id="mock-chat",
        embedding_model_id="mock-embedding",
        embedding_dimensions=2,
    )
    engine = build_engine(settings.database_url)
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            seed_demo_data(session)
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
                    model_id="mock-embedding",
                    dimensions=2,
                    vector=[1.0, 0.0],
                )
            )
    finally:
        engine.dispose()
    chat, embedding = Mock(), Mock()
    embedding.model, embedding.dimensions = "mock-embedding", 2
    embedding.embed_query.return_value = [1.0, 0.0]
    monkeypatch.setattr(run_agent, "Settings", lambda: settings)
    monkeypatch.setattr(run_agent, "build_chat_client", Mock(return_value=chat))
    monkeypatch.setattr(run_agent, "build_embedding_client", Mock(return_value=embedding))
    return chat, embedding, database


def test_preview_never_constructs_clients_opens_database_or_writes_report(
    agent_cli_context, monkeypatch, tmp_path, capsys
):
    engine_factory = Mock(side_effect=AssertionError("预览不能打开数据库"))
    monkeypatch.setattr(run_agent, "build_engine", engine_factory)
    output = tmp_path / "preview.json"
    assert run_agent.main(["--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["demo_user_id"] == "U001"
    assert report["limits"]["max_model_calls"] == 3
    assert report["max_embedding_calls"] == 4
    run_agent.build_chat_client.assert_not_called()
    run_agent.build_embedding_client.assert_not_called()
    engine_factory.assert_not_called()
    assert not output.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--query", " "],
        ["--query", "x" * 1001],
        ["--max-model-calls", "0"],
        ["--max-tool-calls", "-1"],
        ["--max-output-chars", "0"],
    ],
)
def test_invalid_input_fails_before_client_construction(agent_cli_context, arguments, capsys):
    assert run_agent.main(["--run", *arguments]) == 1
    assert capsys.readouterr().out == ""
    run_agent.build_chat_client.assert_not_called()
    run_agent.build_embedding_client.assert_not_called()


def test_cli_runs_real_tools_and_validates_final_answer_without_database_writes(
    agent_cli_context, tmp_path, capsys
):
    chat, embedding, database = agent_cli_context
    original_database = database.read_bytes()
    output = tmp_path / "reports" / "result.json"
    chat.bind_tools.return_value.invoke.side_effect = [
        AIMessage(
            content="",
            tool_calls=[{"id": "order", "name": "lookup_order", "args": {"order_id": "O004"}}],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "policy",
                    "name": "search_order_policies",
                    "args": {"order_id": "O004", "item_id": "O004-I01", "query": "耳机换货"},
                }
            ],
        ),
        AIMessage(
            content=json.dumps(
                {
                    "status": "needs_clarification",
                    "answer": "请补充具体故障，目前仅提供咨询，尚未创建申请。",
                    "citation_chunk_ids": ["digital:v1:000"],
                    "limitations": [],
                }
            )
        ),
    ]
    assert run_agent.main(["--run", "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["answer_validated"] is True
    assert report["answer"]["citation_chunk_ids"] == ["digital:v1:000"]
    assert (report["model_calls"], report["tool_calls"]) == (3, 2)
    assert report["tool_requests"] == [
        {"id": "order", "name": "lookup_order"},
        {"id": "policy", "name": "search_order_policies"},
    ]
    assert all(m["usage_metadata"] is None for m in report["model_responses"])
    embedding.embed_query.assert_called_once_with("耳机换货")
    last_input = chat.bind_tools.return_value.invoke.call_args.args[0]
    results = [m for m in last_input if isinstance(m, ToolMessage)]
    assert json.loads(results[0].content)["data"]["id"] == "O004"
    assert json.loads(results[1].content)["data"][0]["chunk"]["chunk_id"] == "digital:v1:000"
    assert database.read_bytes() == original_database
    assert "api_key" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["invalid_json", "invented_citation", "timeout", "budget"])
def test_cli_failed_run_or_answer_is_saved_as_failure(agent_cli_context, tmp_path, capsys, mode):
    chat, embedding, _ = agent_cli_context
    invoke = chat.bind_tools.return_value.invoke
    arguments = ["--run", "--output", str(tmp_path / "failed.json")]
    if mode == "timeout":
        invoke.side_effect = TimeoutError("private network detail")
    elif mode == "budget":
        arguments += ["--max-model-calls", "1"]
        invoke.return_value = AIMessage(
            content="",
            tool_calls=[{"id": "order", "name": "lookup_order", "args": {"order_id": "O004"}}],
        )
    else:
        invoke.return_value = AIMessage(
            content="not JSON"
            if mode == "invalid_json"
            else json.dumps(
                {
                    "status": "answered",
                    "answer": "模拟政策解释",
                    "citation_chunk_ids": ["invented:v1:000"],
                    "limitations": [],
                }
            )
        )
    assert run_agent.main(arguments) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["answer_validated"] is False
    assert report["answer"] is None
    assert report["model_calls"] == 1
    assert report["tool_calls"] == 0
    assert report["error_code"] == (
        "AGENT_RUN_FAILED" if mode in {"timeout", "budget"} else "ANSWER_INVALID"
    )
    assert report["stop_reason"] == (
        "failed" if mode == "timeout" else "budget_exhausted" if mode == "budget" else "completed"
    )
    assert json.loads((tmp_path / "failed.json").read_text(encoding="utf-8")) == report
    assert "private network detail" not in captured.out + captured.err
    embedding.embed_query.assert_not_called()


def test_missing_database_is_not_created(tmp_path, monkeypatch, capsys):
    database = tmp_path / "missing.db"
    monkeypatch.setattr(
        run_agent,
        "Settings",
        lambda: Settings(_env_file=None, database_url=f"sqlite:///{database.as_posix()}"),
    )
    assert run_agent.main([]) == 1
    assert not database.exists()
    assert capsys.readouterr().out == ""


def test_database_session_rejects_accidental_writes(agent_cli_context, monkeypatch, capsys):
    chat, _, database = agent_cli_context
    original_database = database.read_bytes()

    def attempt_write(session, user_id):
        session.execute(text("DELETE FROM orders"))
        raise AssertionError("数据库必须拒绝写入")

    monkeypatch.setattr(run_agent, "build_order_tools", attempt_write)
    assert run_agent.main(["--run"]) == 1
    assert database.read_bytes() == original_database
    assert capsys.readouterr().out == ""
    chat.bind_tools.assert_not_called()
