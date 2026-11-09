import json
import os
import subprocess
import sys
from datetime import date
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import event
from sqlalchemy.orm import Session

from shopcare.agent import agent
from shopcare.db import build_engine, init_db
from shopcare.db.models import Conversation, PolicyChunkRecord, PolicyEmbeddingRecord, User
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.conversations import create_conversation


@pytest.fixture
def turn_context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'turns.db').as_posix()}")
    try:
        init_db(engine)
        with Session(engine) as session, session.begin():
            session.add_all(
                [User(id="U001", display_name="first"), User(id="U002", display_name="second")]
            )
            session.flush()
            record = create_conversation(session, "U001", date(2026, 11, 2))
            record_id, thread_id = record.id, record.thread_id
        yield engine, record_id, thread_id
    finally:
        engine.dispose()


def test_prepare_turn_commits_running_and_returns_plain_values(turn_context):
    engine, record_id, thread_id = turn_context
    result = agent.prepare_conversation_turn(engine, "U001", record_id)
    assert result == (thread_id, date(2026, 11, 2))
    assert isinstance(result[0], str)
    assert isinstance(result[1], date)
    with Session(engine) as session:
        assert session.get(Conversation, record_id).status == "running"


@pytest.mark.parametrize(
    "user_id, use_existing_id",
    [("U002", True), ("missing-user", True), ("U001", False)],
)
def test_prepare_turn_rejects_inaccessible_conversation(turn_context, user_id, use_existing_id):
    engine, record_id, _ = turn_context
    with pytest.raises(LookupError, match="会话不存在或不可访问"):
        agent.prepare_conversation_turn(
            engine, user_id, record_id if use_existing_id else "missing-conversation"
        )
    with Session(engine) as session:
        assert session.get(Conversation, record_id).status == "ready"


@pytest.mark.parametrize("status", ["running", "failed"])
def test_prepare_turn_rejects_non_ready_conversation(turn_context, status):
    engine, record_id, _ = turn_context
    with Session(engine) as session, session.begin():
        session.get(Conversation, record_id).status = status
    with pytest.raises(RuntimeError, match="当前不可执行"):
        agent.prepare_conversation_turn(engine, "U001", record_id)
    with Session(engine) as session:
        assert session.get(Conversation, record_id).status == status


def test_prepare_turn_rolls_back_when_commit_fails(turn_context, monkeypatch):
    engine, record_id, thread_id = turn_context
    failing_session = Session(engine)

    def reject_commit(session):
        raise RuntimeError("simulated commit failure")

    event.listen(failing_session, "before_commit", reject_commit)
    with monkeypatch.context() as patch:
        patch.setattr(agent, "Session", lambda engine: failing_session)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            agent.prepare_conversation_turn(engine, "U001", record_id)
    with Session(engine) as session:
        assert session.get(Conversation, record_id).status == "ready"
    assert agent.prepare_conversation_turn(engine, "U001", record_id) == (
        thread_id,
        date(2026, 11, 2),
    )


@pytest.fixture
def conversation_graph_context(turn_context, tmp_path):
    engine, _, thread_id = turn_context
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
                model_id="test-model",
                dimensions=2,
                vector=[1.0, 0.0],
            )
        )
    embedding = Mock()
    embedding.model, embedding.dimensions = "test-model", 2
    embedding.embed_query.return_value = [1.0, 0.0]
    return engine, thread_id, embedding, str(tmp_path / "checkpoints.db")


def policy_request(call_id="policy-1"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "search_order_policies",
                "args": {"order_id": "O004", "item_id": "O004-I01", "query": "耳机换货"},
            }
        ],
    )


def answer_message(status="answered", citation_ids=None):
    return AIMessage(
        content=json.dumps(
            {
                "status": status,
                "answer": "请补充具体故障，目前仅提供模拟政策咨询。",
                "citation_chunk_ids": ["digital:v1:000"] if citation_ids is None else citation_ids,
                "limitations": [],
                "policy_quotes": [
                    {"chunk_id": chunk_id, "quote": "模拟政策"}
                    for chunk_id in (["digital:v1:000"] if citation_ids is None else citation_ids)
                ],
            },
            ensure_ascii=False,
        )
    )


def test_execute_graph_restores_history_and_uses_real_tools_for_new_turn(
    conversation_graph_context,
):
    engine, thread_id, embedding, path = conversation_graph_context
    first_client = Mock()
    first_client.bind_tools.return_value.invoke.return_value = answer_message(
        "needs_clarification", []
    )
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        first = agent.execute_conversation_graph(
            session,
            first_client,
            embedding,
            saver,
            "U001",
            thread_id,
            date(2026, 11, 2),
            "订单O004的O004-I01耳机坏了，能换货吗？",
        )
        assert first.status == "needs_clarification"
        assert session.get(User, "U001") is not None  # 函数没有关闭调用方Session。
    embedding.embed_query.assert_not_called()
    engine.dispose()

    second_client = Mock()
    second_client.bind_tools.return_value.invoke.side_effect = [policy_request(), answer_message()]
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        second = agent.execute_conversation_graph(
            session,
            second_client,
            embedding,
            saver,
            "U001",
            thread_id,
            date(2026, 11, 2),
            " 左边没声音 ",
        )
        assert second.citation_chunk_ids == ["digital:v1:000"]
        model_input = second_client.bind_tools.return_value.invoke.call_args_list[0].args[0]
        assert [m.content for m in model_input if isinstance(m, HumanMessage)] == [
            "订单O004的O004-I01耳机坏了，能换货吗？",
            "左边没声音",
        ]
        assert len([m for m in model_input if isinstance(m, SystemMessage)]) == 2
        graph = agent.build_agent_graph(second_client, [], checkpointer=saver)
        saved = graph.get_state({"configurable": {"thread_id": thread_id}}).values
        assert len(saved["messages"]) == 7
        assert saved["model_calls"] == 2  # 本轮预算重置，没有累加第一轮的1次。
        assert saved["tool_calls"] == 1
        assert session.get(User, "U001") is not None
    embedding.embed_query.assert_called_once_with("耳机换货")


def test_execute_graph_rejects_old_citation_and_cannot_reuse_invalid_saved_answer(
    conversation_graph_context,
):
    engine, thread_id, embedding, path = conversation_graph_context
    client = Mock()
    client.bind_tools.return_value.invoke.side_effect = [policy_request(), answer_message()]
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        agent.execute_conversation_graph(
            session, client, embedding, saver, "U001", thread_id, date(2026, 11, 2), "查询耳机政策"
        )

        # 旧片段确实存在于历史中，但本轮没有检索，不能用于本轮引用。
        stale_client = Mock()
        stale_client.bind_tools.return_value.invoke.return_value = answer_message()
        with pytest.raises(ValueError, match="摘录不属于本轮检索证据"):
            agent.execute_conversation_graph(
                session,
                stale_client,
                embedding,
                saver,
                "U001",
                thread_id,
                date(2026, 11, 2),
                "再解释一下",
            )
        assert stale_client.bind_tools.return_value.invoke.call_count == 3

        # 即使持久化的stop_reason为completed，也必须重新检查上轮自身的证据范围。
        next_client = Mock()
        with pytest.raises(ValueError, match="摘录不属于本轮检索证据"):
            agent.execute_conversation_graph(
                session, next_client, embedding, saver, "U001", thread_id, date(2026, 11, 2), "继续"
            )
        next_client.bind_tools.return_value.invoke.assert_not_called()
    embedding.embed_query.assert_called_once()


def test_execute_graph_repairs_stale_citation_with_fresh_tools_within_turn_budget(
    conversation_graph_context,
):
    engine, thread_id, embedding, path = conversation_graph_context
    first_client = Mock()
    first_client.bind_tools.return_value.invoke.side_effect = [policy_request(), answer_message()]
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        agent.execute_conversation_graph(
            session,
            first_client,
            embedding,
            saver,
            "U001",
            thread_id,
            date(2026, 11, 2),
            "耳机坏了",
        )
    client = Mock()
    request = policy_request("new-policy")
    request.tool_calls.append(
        {"id": "new-order", "name": "lookup_order", "args": {"order_id": "O004"}}
    )
    # 第一次直接沿用旧引用，应触发纠正；其后才真正检索本轮证据。
    client.bind_tools.return_value.invoke.side_effect = [
        answer_message(),
        request,
        answer_message(),
    ]
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        answer = agent.execute_conversation_graph(
            session, client, embedding, saver, "U001", thread_id, date(2026, 11, 2), "左边没声音"
        )
        assert answer.citation_chunk_ids == ["digital:v1:000"]
        invoke = client.bind_tools.return_value.invoke
        assert invoke.call_count == 3
        assert client.bind_tools.call_args.kwargs == {"tool_choice": "search_order_policies"}
        assert "片段ID为：[]" in invoke.call_args_list[0].args[0][-1].content
        assert "未通过" in invoke.call_args_list[1].args[0][-2].content
        assert '片段ID为：["digital:v1:000"]' in invoke.call_args_list[2].args[0][-1].content
        saved = saver.get_tuple({"configurable": {"thread_id": thread_id}}).checkpoint[
            "channel_values"
        ]
        assert saved["model_calls"] == 3
        assert saved["tool_calls"] == 2
        order_results = [
            m
            for m in saved["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == "new-order"
        ]
        assert json.loads(order_results[0].content)["data"]["days_since_delivery"] == 3
    assert embedding.embed_query.call_count == 2


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "budget", "retrieval"])
def test_execute_graph_rejects_failed_run_and_blocks_following_model_call(
    conversation_graph_context, failure
):
    engine, thread_id, embedding, path = conversation_graph_context
    client = Mock()
    invoke = client.bind_tools.return_value.invoke
    if failure == "timeout":
        invoke.side_effect = TimeoutError("model timeout")
    elif failure == "invalid_json":
        invoke.return_value = AIMessage(content="invalid JSON")
    elif failure == "retrieval":
        invoke.return_value = policy_request()
        embedding.embed_query.side_effect = TimeoutError("embedding timeout")
    else:
        invoke.side_effect = [policy_request(f"policy-{index}") for index in range(3)]
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        with pytest.raises(ValueError):
            agent.execute_conversation_graph(
                session,
                client,
                embedding,
                saver,
                "U001",
                thread_id,
                date(2026, 11, 2),
                "查询耳机政策",
            )
        assert invoke.call_count == (3 if failure in {"budget", "invalid_json"} else 1)
        next_client = Mock()
        with pytest.raises(ValueError):
            agent.execute_conversation_graph(
                session, next_client, embedding, saver, "U001", thread_id, date(2026, 11, 2), "继续"
            )
        next_client.bind_tools.return_value.invoke.assert_not_called()


def test_execute_graph_rejects_checkpoint_with_pending_node(conversation_graph_context):
    engine, thread_id, embedding, path = conversation_graph_context
    config = {"configurable": {"thread_id": thread_id}}
    broken_client = Mock()
    broken_client.bind_tools.return_value.invoke.side_effect = RuntimeError("model adapter bug")
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        graph = agent.build_agent_graph(broken_client, [], checkpointer=saver)
        with pytest.raises(RuntimeError, match="model adapter bug"):
            graph.invoke(
                {
                    "messages": agent.build_agent_messages("耳机坏了", date(2026, 11, 2)),
                    "model_calls": 0,
                    "tool_calls": 0,
                    "stop_reason": "running",
                },
                config=config,
            )
        assert graph.get_state(config).next
        next_client = Mock()
        with pytest.raises(ValueError, match="上轮执行未正常结束"):
            agent.execute_conversation_graph(
                session, next_client, embedding, saver, "U001", thread_id, date(2026, 11, 2), "继续"
            )
        next_client.bind_tools.return_value.invoke.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [
        {"checkpointer": None},
        {"thread_id": " "},
        {"thread_id": None},
        {"query": " "},
        {"query": "x" * 1001},
        {"query": None},
    ],
)
def test_execute_graph_validates_input_before_binding_model(conversation_graph_context, invalid):
    engine, thread_id, embedding, path = conversation_graph_context
    client = Mock()
    with SqliteSaver.from_conn_string(path) as saver, Session(engine) as session:
        arguments = {
            "session": session,
            "chat_client": client,
            "embedding_client": embedding,
            "checkpointer": saver,
            "user_id": "U001",
            "thread_id": thread_id,
            "as_of": date(2026, 11, 2),
            "query": "查询耳机政策",
        }
        arguments.update(invalid)
        with pytest.raises(ValueError):
            agent.execute_conversation_graph(**arguments)
        client.bind_tools.assert_not_called()
        embedding.embed_query.assert_not_called()


@pytest.fixture
def run_context(conversation_graph_context, turn_context):
    engine, _, embedding, path = conversation_graph_context
    with SqliteSaver.from_conn_string(path) as saver:
        yield {
            "engine": engine,
            "chat_client": Mock(),
            "embedding_client": embedding,
            "checkpointer": saver,
            "user_id": "U001",
            "conversation_id": turn_context[1],
            "query": "订单O004的O004-I01耳机坏了，能换货吗？",
        }


def test_run_turn_commits_before_model_and_returns_ready_for_next_turn(run_context):
    engine = run_context["engine"]
    record_id = run_context["conversation_id"]

    def first_reply(messages):
        # 模型调用时，其他连接已能看到running，并能完成自己的写事务。
        with Session(engine) as session, session.begin():
            assert session.get(Conversation, record_id).status == "running"
            session.add(User(id="U003", display_name="independent writer"))
        return answer_message("needs_clarification", [])

    run_context["chat_client"].bind_tools.return_value.invoke.side_effect = first_reply
    first = agent.run_conversation_turn(**run_context)
    assert first.status == "needs_clarification"
    with Session(engine) as session:
        record = session.get(Conversation, record_id)
        assert record.status == "ready"
        assert record.as_of == date(2026, 11, 2)
        original_thread_id = record.thread_id
        assert session.get(User, "U003") is not None

    client = Mock()
    client.bind_tools.return_value.invoke.side_effect = [policy_request(), answer_message()]
    run_context["chat_client"] = client
    run_context["query"] = "左边没声音"
    second = agent.run_conversation_turn(**run_context)
    assert second.citation_chunk_ids == ["digital:v1:000"]
    with Session(engine) as session:
        record = session.get(Conversation, record_id)
        assert record.status == "ready"
        assert record.thread_id == original_thread_id
        assert record.as_of == date(2026, 11, 2)


@pytest.mark.parametrize(
    "failure", ["timeout", "invalid_json", "retrieval", "budget", "bind", "bad_quote"]
)
def test_run_turn_marks_failure_and_blocks_following_request(run_context, failure):
    client = run_context["chat_client"]
    invoke = client.bind_tools.return_value.invoke
    if failure == "timeout":
        invoke.side_effect = TimeoutError("model timeout")
    elif failure == "invalid_json":
        invoke.return_value = AIMessage(content="invalid JSON")
    elif failure == "retrieval":
        invoke.return_value = policy_request()
        run_context["embedding_client"].embed_query.side_effect = TimeoutError("retrieval timeout")
    elif failure == "bind":
        client.bind_tools.side_effect = RuntimeError("binding failed")
    elif failure == "bad_quote":
        wrong = json.loads(answer_message().content)
        wrong["policy_quotes"][0]["quote"] = "必须平台审核"
        invoke.side_effect = [
            policy_request(),
            AIMessage(content=json.dumps(wrong)),
            AIMessage(content=json.dumps(wrong)),
        ]
    else:
        invoke.side_effect = [policy_request(f"policy-{index}") for index in range(3)]
    with pytest.raises(RuntimeError if failure == "bind" else ValueError):
        agent.run_conversation_turn(**run_context)
    with Session(run_context["engine"]) as session:
        assert session.get(Conversation, run_context["conversation_id"]).status == "failed"

    next_client = Mock()
    run_context["chat_client"] = next_client
    with pytest.raises(RuntimeError, match="当前不可执行"):
        agent.run_conversation_turn(**run_context)
    next_client.bind_tools.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [{"query": " "}, {"query": "x" * 1001}, {"query": None}, {"checkpointer": None}],
)
def test_run_turn_rejects_bad_input_before_changing_status(run_context, invalid):
    arguments = {**run_context, **invalid}
    with pytest.raises(ValueError):
        agent.run_conversation_turn(**arguments)
    with Session(run_context["engine"]) as session:
        assert session.get(Conversation, run_context["conversation_id"]).status == "ready"
    run_context["chat_client"].bind_tools.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [
        {"user_id": "U002"},
        {"user_id": "missing-user"},
        {"conversation_id": "missing-conversation"},
    ],
)
def test_run_turn_rejects_inaccessible_conversation_before_checkpoint(run_context, invalid):
    arguments = {**run_context, **invalid}
    with pytest.raises(LookupError, match="会话不存在或不可访问"):
        agent.run_conversation_turn(**arguments)
    with Session(run_context["engine"]) as session:
        record = session.get(Conversation, run_context["conversation_id"])
        assert record.status == "ready"
        config = {"configurable": {"thread_id": record.thread_id}}
    assert run_context["checkpointer"].get_tuple(config) is None
    run_context["chat_client"].bind_tools.assert_not_called()


@pytest.mark.parametrize("status", ["running", "failed"])
def test_run_turn_rejects_non_ready_conversation_without_finishing_it(run_context, status):
    with Session(run_context["engine"]) as session, session.begin():
        session.get(Conversation, run_context["conversation_id"]).status = status
    with pytest.raises(RuntimeError, match="当前不可执行"):
        agent.run_conversation_turn(**run_context)
    with Session(run_context["engine"]) as session:
        assert session.get(Conversation, run_context["conversation_id"]).status == status
    run_context["chat_client"].bind_tools.assert_not_called()


def test_run_turn_preserves_original_error_if_failure_status_cannot_be_saved(
    run_context, monkeypatch
):
    original_error = ValueError("original validation failure")
    monkeypatch.setattr(agent, "execute_conversation_graph", Mock(side_effect=original_error))
    finish = Mock(side_effect=RuntimeError("finish unavailable"))
    monkeypatch.setattr(agent, "finish_conversation_turn", finish)
    with pytest.raises(ValueError) as caught:
        agent.run_conversation_turn(**run_context)
    assert caught.value is original_error
    assert any("失败状态未能写入" in note for note in caught.value.__notes__)
    assert finish.call_count == 1
    assert finish.call_args.kwargs == {"succeeded": False}
    with Session(run_context["engine"]) as session:
        assert session.get(Conversation, run_context["conversation_id"]).status == "running"


def test_run_turn_does_not_return_answer_or_finalize_twice_if_success_finish_fails(
    run_context, monkeypatch
):
    run_context["chat_client"].bind_tools.return_value.invoke.return_value = answer_message(
        "needs_clarification", []
    )
    finish = Mock(side_effect=RuntimeError("finish unavailable"))
    monkeypatch.setattr(agent, "finish_conversation_turn", finish)
    with pytest.raises(RuntimeError, match="finish unavailable"):
        agent.run_conversation_turn(**run_context)
    assert finish.call_count == 1
    assert finish.call_args.kwargs == {"succeeded": True}
    with Session(run_context["engine"]) as session:
        assert session.get(Conversation, run_context["conversation_id"]).status == "running"


def test_chat_cli_continues_in_new_python_process(conversation_graph_context):
    engine, _, _, path = conversation_graph_context
    # 两次独立解释器进程运行真实CLI/业务服务/检查点；仅模型客户端替换为Mock。
    code = r"""
import json
import sys
from unittest.mock import Mock
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from shopcare.core.config import Settings
from shopcare.scripts import chat_agent

client, embedding = Mock(), Mock()
embedding.model, embedding.dimensions = "test-model", 2
embedding.embed_query.return_value = [1.0, 0.0]
mode = sys.argv[3]
def reply(messages):
    if mode == "first":
        return AIMessage(content=json.dumps({
            "status": "needs_clarification", "answer": "请补充具体故障。",
            "citation_chunk_ids": [], "limitations": [], "policy_quotes": []
        }))
    humans = [m.content for m in messages if isinstance(m, HumanMessage)]
    assert humans == ["订单O004的O004-I01耳机坏了，能换货吗？", "左边没声音"]
    if not any(isinstance(m, ToolMessage) for m in messages):
        return AIMessage(content="", tool_calls=[{
            "id": "policy", "name": "search_order_policies",
            "args": {"order_id": "O004", "item_id": "O004-I01", "query": "耳机换货"}
        }])
    return AIMessage(content=json.dumps({
        "status": "answered", "answer": "这是模拟政策说明，尚未创建任何申请。",
        "citation_chunk_ids": ["digital:v1:000"], "limitations": [],
        "policy_quotes": [{"chunk_id": "digital:v1:000", "quote": "模拟政策"}]
    }))
client.bind_tools.return_value.invoke.side_effect = reply
chat_agent.Settings = lambda: Settings(
    _env_file=None, database_url=sys.argv[1], llm_model_id="mock-chat",
    embedding_model_id="test-model", embedding_dimensions=2
)
chat_agent.build_chat_client = lambda settings: client
chat_agent.build_embedding_client = lambda settings: embedding
arguments = ["--run", "--checkpoint-db", sys.argv[2]]
if mode == "second":
    arguments += ["--conversation-id", sys.argv[4], "--query", "左边没声音"]
result = chat_agent.main(arguments)
assert client.bind_tools.return_value.invoke.call_count == (1 if mode == "first" else 2)
assert embedding.embed_query.call_count == (0 if mode == "first" else 1)
raise SystemExit(result)
"""
    arguments = [sys.executable, "-W", "error", "-c", code, str(engine.url), path]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    first = subprocess.run(
        [*arguments, "first"], capture_output=True, encoding="utf-8", env=environment, timeout=30
    )
    assert first.returncode == 0, first.stderr
    first_report = json.loads(first.stdout)
    assert first_report["conversation_status"] == "ready"
    assert first_report["answer"]["status"] == "needs_clarification"
    record_id = first_report["conversation_id"]
    second = subprocess.run(
        [*arguments, "second", record_id],
        capture_output=True,
        encoding="utf-8",
        env=environment,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr
    second_report = json.loads(second.stdout)
    assert second_report["conversation_id"] == record_id
    assert second_report["conversation_status"] == "ready"
    assert second_report["answer"]["citation_chunk_ids"] == ["digital:v1:000"]
    with Session(engine) as session:
        assert session.get(Conversation, record_id).status == "ready"
