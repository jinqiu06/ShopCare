import json
from datetime import date
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from openai import OpenAIError
from sqlalchemy.orm import Session

from shopcare.agent.graph import build_agent_graph
from shopcare.agent.prompts import build_agent_messages
from shopcare.agent.results import parse_agent_answer
from shopcare.db import build_engine, init_db
from shopcare.db.models import PolicyChunkRecord, PolicyEmbeddingRecord
from shopcare.scripts.seed import seed_demo_data
from shopcare.tools.orders import build_order_tools
from shopcare.tools.policy import build_policy_tools


@pytest.fixture
def graph_context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'graph.db').as_posix()}")
    init_db(engine)
    try:
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
        with Session(engine) as session:
            tools = build_order_tools(session, "U001") + build_policy_tools(
                session, embedding, "U001", date(2026, 11, 2)
            )
            yield tools, embedding
    finally:
        engine.dispose()


def initial_state():
    return {
        "messages": [HumanMessage(content="订单O004的O004-I01耳机坏了，能换货吗？")],
        "model_calls": 0,
        "tool_calls": 0,
        "stop_reason": "running",
    }


def tool_request(name="lookup_order", args=None, call_id="call-1"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {"order_id": "O004"} if args is None else args,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_graph_selects_next_tool_from_prior_result_and_finishes(graph_context):
    tools, embedding = graph_context
    client = Mock()

    def reply(messages):
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if not results:
            return tool_request()
        if len(results) == 1:
            order = json.loads(results[0].content)["data"]
            assert order["id"] == "O004"
            return tool_request(
                "search_order_policies",
                {"order_id": order["id"], "item_id": "O004-I01", "query": "耳机换货"},
                "call-2",
            )
        evidence = json.loads(results[-1].content)["data"]
        assert evidence[0]["chunk"]["chunk_id"] == "digital:v1:000"
        return AIMessage(content="请补充具体故障，目前未创建申请。")

    client.bind_tools.return_value.invoke.side_effect = reply
    source = initial_state()
    result = build_agent_graph(client, tools).invoke(source)
    assert result["stop_reason"] == "completed"
    assert (result["model_calls"], result["tool_calls"]) == (3, 2)
    assert [message.type for message in result["messages"]] == [
        "human",
        "ai",
        "tool",
        "ai",
        "tool",
        "ai",
    ]
    assert [m.tool_call_id for m in result["messages"] if isinstance(m, ToolMessage)] == [
        "call-1",
        "call-2",
    ]
    assert source["model_calls"] == source["tool_calls"] == 0
    assert len(source["messages"]) == 1
    embedding.embed_query.assert_called_once_with("耳机换货")


@pytest.mark.parametrize(
    "citation_ids, valid",
    [
        (["digital:v1:000"], True),
        (["invented:v1:000"], False),
        (["digital:v1:000", "digital:v1:000"], False),
        ([], False),
    ],
)
def test_graph_final_answer_is_validated_against_actual_policy_results(
    graph_context, citation_ids, valid
):
    tools, _ = graph_context
    client = Mock()
    client.bind_tools.return_value.invoke.side_effect = [
        tool_request(
            "search_order_policies",
            {"order_id": "O004", "item_id": "O004-I01", "query": "耳机换货"},
        ),
        AIMessage(
            content=json.dumps(
                {
                    "status": "answered",
                    "answer": "这是模拟政策说明，目前仅提供咨询，尚未创建申请。",
                    "citation_chunk_ids": citation_ids,
                    "limitations": [],
                }
            )
        ),
    ]
    source = initial_state()
    source["messages"] = build_agent_messages("订单O004的O004-I01耳机如何换货？", date(2026, 11, 2))
    result = build_agent_graph(client, tools).invoke(source)
    assert result["stop_reason"] == "completed"
    if valid:
        assert parse_agent_answer(result).citation_chunk_ids == citation_ids
    else:
        with pytest.raises(ValueError):
            parse_agent_answer(result)


def test_graph_direct_answer_skips_tools(graph_context):
    tools, embedding = graph_context
    client = Mock()
    client.bind_tools.return_value.invoke.return_value = AIMessage(content="请提供具体故障表现。")
    result = build_agent_graph(client, tools).invoke(initial_state())
    assert result["stop_reason"] == "completed"
    assert (result["model_calls"], result["tool_calls"]) == (1, 0)
    embedding.embed_query.assert_not_called()


@pytest.mark.parametrize("invalid_args", [False, True])
def test_graph_tool_error_is_visible_to_next_model_call(graph_context, invalid_args):
    tools, _ = graph_context
    client = Mock()
    first = tool_request(args={}) if invalid_args else tool_request("unknown_tool")

    def reply(messages):
        if not isinstance(messages[-1], ToolMessage):
            return first
        payload = json.loads(messages[-1].content)
        assert payload["error"]["code"] == ("INVALID_ARGUMENT" if invalid_args else "UNKNOWN_TOOL")
        return AIMessage(content="请核对订单信息。")

    client.bind_tools.return_value.invoke.side_effect = reply
    result = build_agent_graph(client, tools).invoke(initial_state())
    assert result["stop_reason"] == "completed"
    assert (result["model_calls"], result["tool_calls"]) == (2, 1)


def test_graph_model_budget_stops_before_unusable_last_tool_batch(graph_context):
    tools, embedding = graph_context
    client = Mock()
    client.bind_tools.return_value.invoke.side_effect = [
        tool_request(call_id=f"call-{i}") for i in range(3)
    ]
    result = build_agent_graph(client, tools, max_model_calls=2).invoke(initial_state())
    assert result["stop_reason"] == "budget_exhausted"
    assert (result["model_calls"], result["tool_calls"]) == (2, 1)
    assert client.bind_tools.return_value.invoke.call_count == 2
    embedding.embed_query.assert_not_called()


def test_graph_tool_budget_stops_without_partial_batch(graph_context):
    tools, _ = graph_context
    client = Mock()
    message = tool_request()
    message.tool_calls.append(
        {
            "name": "lookup_order",
            "args": {"order_id": "O004"},
            "id": "call-2",
            "type": "tool_call",
        }
    )
    client.bind_tools.return_value.invoke.return_value = message
    result = build_agent_graph(client, tools, max_tool_calls=1).invoke(initial_state())
    assert result["stop_reason"] == "budget_exhausted"
    assert result["tool_calls"] == 0
    assert not any(isinstance(m, ToolMessage) for m in result["messages"])
    client.bind_tools.return_value.invoke.assert_called_once()


@pytest.mark.parametrize("failure", ["timeout", "upstream", "bad_message"])
def test_graph_model_failure_ends_without_more_requests(graph_context, failure):
    tools, _ = graph_context
    client = Mock()
    invoke = client.bind_tools.return_value.invoke
    if failure == "bad_message":
        invoke.return_value = HumanMessage(content="wrong response type")
    else:
        invoke.side_effect = (
            TimeoutError("private timeout") if failure == "timeout" else OpenAIError("private")
        )
    result = build_agent_graph(client, tools).invoke(initial_state())
    assert result["stop_reason"] == "failed"
    assert (result["model_calls"], result["tool_calls"]) == (1, 0)
    assert len(result["messages"]) == 1
    invoke.assert_called_once()


@pytest.mark.parametrize("failure", ["infrastructure", "exception", "oversized"])
def test_graph_tool_failure_never_sends_incomplete_history_to_model(
    graph_context, monkeypatch, failure
):
    from shopcare.agent import nodes

    tools, _ = graph_context
    client = Mock()
    message = tool_request()
    message.tool_calls.append(
        {
            "name": "lookup_order",
            "args": {"order_id": "O004"},
            "id": "call-2",
            "type": "tool_call",
        }
    )
    client.bind_tools.return_value.invoke.return_value = message
    execute = Mock()
    if failure == "exception":
        execute.side_effect = RuntimeError("private detail")
    elif failure == "oversized":
        execute.return_value = {"ok": True, "data": "x" * 200}
    else:
        execute.return_value = {"ok": False, "error": {"code": "TEMPORARILY_UNAVAILABLE"}}
    monkeypatch.setattr(nodes, "execute_tool", execute)
    result = build_agent_graph(client, tools, max_output_chars=100).invoke(initial_state())
    assert result["stop_reason"] == "failed"
    assert result["tool_calls"] == 1
    client.bind_tools.return_value.invoke.assert_called_once()
    execute.assert_called_once()


@pytest.mark.parametrize("parameter", ["max_model_calls", "max_tool_calls", "max_output_chars"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_graph_rejects_invalid_limits_before_binding_model(parameter, value):
    client = Mock()
    with pytest.raises(ValueError):
        build_agent_graph(client, [], **{parameter: value})
    client.bind_tools.assert_not_called()


def test_graph_rejects_duplicate_tool_names(graph_context):
    tools, _ = graph_context
    client = Mock()
    with pytest.raises(ValueError, match="重复"):
        build_agent_graph(client, [tools[0], tools[0]])
    client.bind_tools.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"messages": []},
        {"messages": [AIMessage(content="not a user message")]},
        {"model_calls": True},
        {"model_calls": -1},
        {"tool_calls": 1},
        {"stop_reason": "failed"},
    ],
)
def test_graph_rejects_invalid_initial_state_before_model_call(override):
    client = Mock()
    graph = build_agent_graph(client, [])
    state = initial_state()
    state.update(override)
    with pytest.raises(ValueError):
        graph.invoke(state)
    client.bind_tools.return_value.invoke.assert_not_called()


def test_graph_large_model_budget_uses_matching_graph_step_limit(graph_context):
    tools, _ = graph_context
    client = Mock()
    client.bind_tools.return_value.invoke.side_effect = [
        *(tool_request(call_id=f"call-{i}") for i in range(13)),
        AIMessage(content="查询结束。"),
    ]
    result = build_agent_graph(client, tools, max_model_calls=14, max_tool_calls=14).invoke(
        initial_state()
    )
    assert result["stop_reason"] == "completed"
    assert (result["model_calls"], result["tool_calls"]) == (14, 13)


def test_graph_sqlite_checkpoint_survives_reopening_and_appends_new_turn(tmp_path):
    path = str(tmp_path / "checkpoints.db")
    config = {"configurable": {"thread_id": "conversation-thread"}}
    client = Mock()
    client.bind_tools.return_value.invoke.return_value = AIMessage(content="具体是什么故障？")
    with SqliteSaver.from_conn_string(path) as saver:
        graph = build_agent_graph(client, [], checkpointer=saver)
        first = graph.invoke(initial_state(), config=config)
        assert first["stop_reason"] == "completed"
        assert graph.get_state(config).next == ()

    new_client = Mock()
    new_client.bind_tools.return_value.invoke.return_value = AIMessage(content="已收到补充信息。")
    with SqliteSaver.from_conn_string(path) as saver:
        graph = build_agent_graph(new_client, [], checkpointer=saver)
        restored = graph.get_state(config).values
        assert [m.content for m in restored["messages"]] == [
            "订单O004的O004-I01耳机坏了，能换货吗？",
            "具体是什么故障？",
        ]
        new_turn = initial_state()
        # 只提交本轮新消息和零计数；旧消息由保存器加载，不能重复提交整段历史。
        new_turn["messages"] = [HumanMessage(content="左边没声音")]
        second = graph.invoke(new_turn, config=config)
        assert [m.content for m in second["messages"]] == [
            "订单O004的O004-I01耳机坏了，能换货吗？",
            "具体是什么故障？",
            "左边没声音",
            "已收到补充信息。",
        ]
        assert second["model_calls"] == 1
        assert second["tool_calls"] == 0
        assert second["stop_reason"] == "completed"
        model_input = new_client.bind_tools.return_value.invoke.call_args.args[0]
        assert [m.content for m in model_input] == [m.content for m in second["messages"][:-1]]


def test_graph_sqlite_checkpoint_separates_thread_histories(tmp_path):
    client = Mock()
    client.bind_tools.return_value.invoke.return_value = AIMessage(content="请补充信息。")
    first_config = {"configurable": {"thread_id": "first-thread"}}
    second_config = {"configurable": {"thread_id": "second-thread"}}
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        graph = build_agent_graph(client, [], checkpointer=saver)
        graph.invoke(initial_state(), config=first_config)
        assert graph.get_state(second_config).values == {}
        second_input = initial_state()
        second_input["messages"] = [HumanMessage(content="另一个会话的问题")]
        graph.invoke(second_input, config=second_config)
        received = client.bind_tools.return_value.invoke.call_args.args[0]
        assert [m.content for m in received] == ["另一个会话的问题"]
        assert len(graph.get_state(first_config).values["messages"]) == 2
