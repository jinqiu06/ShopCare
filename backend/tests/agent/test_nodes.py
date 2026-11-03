from unittest.mock import Mock

import pytest
from httpx import Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import APITimeoutError, OpenAIError

from shopcare.agent.nodes import build_model_node, build_tool_node


@pytest.mark.parametrize(
    "failure", ["execution", "serialization", "nan", "wrong_type", "oversized"]
)
def test_tool_node_stops_on_unusable_output_and_counts_attempt(monkeypatch, failure):
    from shopcare.agent import nodes

    execute = Mock()
    if failure == "execution":
        execute.side_effect = RuntimeError("private database detail")
    else:
        execute.return_value = {
            "serialization": {"ok": True, "data": {object()}},
            "nan": {"ok": True, "data": float("nan")},
            "wrong_type": [],
            "oversized": {"ok": True, "data": "x" * 200},
        }[failure]
    monkeypatch.setattr(nodes, "execute_tool", execute)
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "lookup_order", "args": {}, "id": f"call-{i}", "type": "tool_call"}
            for i in range(2)
        ],
    )
    state = {"messages": [message], "model_calls": 1, "tool_calls": 1, "stop_reason": "running"}
    assert build_tool_node({}, max_output_chars=100)(state) == {
        "messages": [],
        "tool_calls": 2,
        "stop_reason": "failed",
    }
    execute.assert_called_once()
    assert state["tool_calls"] == 1
    assert len(state["messages"]) == 1


@pytest.mark.parametrize("code", ["TEMPORARILY_UNAVAILABLE", "RETRIEVAL_UNAVAILABLE"])
def test_tool_node_infrastructure_failure_returns_error_and_skips_remaining_tools(
    monkeypatch, code
):
    import json

    from shopcare.agent import nodes

    payload = {"ok": False, "error": {"code": code, "message": "暂时不可用"}}
    execute = Mock(return_value=payload)
    monkeypatch.setattr(nodes, "execute_tool", execute)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "lookup_order", "args": {}, "id": f"call-{i}", "type": "tool_call"}
                    for i in range(2)
                ],
            )
        ],
        "model_calls": 1,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    result = build_tool_node({})(state)
    assert result["stop_reason"] == "failed"
    assert result["tool_calls"] == 1
    assert len(result["messages"]) == 1
    assert result["messages"][0].tool_call_id == "call-0"
    assert json.loads(result["messages"][0].content) == payload
    execute.assert_called_once()


@pytest.mark.parametrize("over_limit", [False, True])
def test_tool_node_output_size_boundary_is_serialized_character_count(monkeypatch, over_limit):
    import json

    from shopcare.agent import nodes

    payload = {"ok": True, "data": "中文结果"}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    monkeypatch.setattr(nodes, "execute_tool", Mock(return_value=payload))
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "lookup_order", "args": {}, "id": "call-1", "type": "tool_call"}
                ],
            )
        ],
        "model_calls": 1,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    result = build_tool_node({}, max_output_chars=len(encoded) - int(over_limit))(state)
    assert result["tool_calls"] == 1
    assert result["stop_reason"] == ("failed" if over_limit else "running")
    assert [message.content for message in result["messages"]] == ([] if over_limit else [encoded])


@pytest.mark.parametrize("failure", ["execution", "serialization", "oversized"])
def test_tool_node_retains_completed_results_before_later_failure(monkeypatch, failure):
    from shopcare.agent import nodes

    failed_result = {
        "execution": RuntimeError("private detail"),
        "serialization": {"ok": True, "data": {object()}},
        "oversized": {"ok": True, "data": "x" * 200},
    }[failure]
    execute = Mock(side_effect=[{"ok": True, "data": "first"}, failed_result])
    monkeypatch.setattr(nodes, "execute_tool", execute)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "lookup_order", "args": {}, "id": f"call-{i}", "type": "tool_call"}
                    for i in range(3)
                ],
            )
        ],
        "model_calls": 1,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    result = build_tool_node({}, max_output_chars=100)(state)
    assert result["stop_reason"] == "failed"
    assert result["tool_calls"] == 2
    assert len(result["messages"]) == 1
    assert result["messages"][0].tool_call_id == "call-0"
    assert execute.call_count == 2


@pytest.mark.parametrize(
    "invalid", ["empty_history", "missing_id", "empty_id", "blank_id", "duplicate_id", "wrong_type"]
)
def test_tool_node_rejects_invalid_call_ids_before_any_execution(monkeypatch, invalid):
    from shopcare.agent import nodes

    execute = Mock()
    monkeypatch.setattr(nodes, "execute_tool", execute)
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "lookup_order", "args": {}, "id": "call-1", "type": "tool_call"},
            {"name": "lookup_order", "args": {}, "id": "call-2", "type": "tool_call"},
        ],
    )
    if invalid == "missing_id":
        message.tool_calls[1].pop("id")
    elif invalid != "empty_history":
        message.tool_calls[1]["id"] = {
            "empty_id": "",
            "blank_id": "   ",
            "duplicate_id": "call-1",
            "wrong_type": [],
        }[invalid]
    state = {
        "messages": [] if invalid == "empty_history" else [message],
        "model_calls": 1,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    assert build_tool_node({})(state) == {"stop_reason": "failed"}
    execute.assert_not_called()


@pytest.mark.parametrize("before", [0, 2])
def test_tool_node_returns_matching_messages_and_counts_batch(monkeypatch, before):
    import json

    from shopcare.agent import nodes

    requests = [
        {"name": "lookup_order", "args": {"order_id": "O004"}, "id": "call-1", "type": "tool_call"},
        {"name": "unknown_tool", "args": {}, "id": "call-2", "type": "tool_call"},
    ]
    outputs = [
        {"ok": True, "data": {"id": "O004"}},
        {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": "工具不存在"}},
    ]
    execute = Mock(side_effect=outputs)
    monkeypatch.setattr(nodes, "execute_tool", execute)
    registry = {}
    state = {
        "messages": [AIMessage(content="", tool_calls=requests)],
        "model_calls": 1,
        "tool_calls": before,
        "stop_reason": "running",
    }
    result = build_tool_node(registry, max_tool_calls=4)(state)
    assert result["tool_calls"] == before + 2
    assert result["stop_reason"] == "running"
    assert all(isinstance(message, ToolMessage) for message in result["messages"])
    assert [message.tool_call_id for message in result["messages"]] == ["call-1", "call-2"]
    assert [json.loads(message.content) for message in result["messages"]] == outputs
    assert execute.call_count == 2
    assert execute.call_args_list[0].args == (registry, "lookup_order", {"order_id": "O004"})
    assert execute.call_args_list[1].args == (registry, "unknown_tool", {})
    assert state["tool_calls"] == before
    assert len(state["messages"]) == 1


@pytest.mark.parametrize("before", [3, 4])
def test_tool_node_rejects_entire_batch_over_budget(monkeypatch, before):
    from shopcare.agent import nodes

    execute = Mock()
    monkeypatch.setattr(nodes, "execute_tool", execute)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup_order",
                        "args": {},
                        "id": f"call-{number}",
                        "type": "tool_call",
                    }
                    for number in range(2)
                ],
            )
        ],
        "model_calls": 1,
        "tool_calls": before,
        "stop_reason": "running",
    }
    assert build_tool_node({}, max_tool_calls=4)(state) == {"stop_reason": "budget_exhausted"}
    execute.assert_not_called()


@pytest.mark.parametrize("message", [HumanMessage(content="查询订单"), AIMessage(content="已回答")])
def test_tool_node_rejects_messages_without_tool_requests(monkeypatch, message):
    from shopcare.agent import nodes

    execute = Mock()
    monkeypatch.setattr(nodes, "execute_tool", execute)
    state = {"messages": [message], "model_calls": 1, "tool_calls": 0, "stop_reason": "running"}
    assert build_tool_node({})(state) == {"stop_reason": "failed"}
    execute.assert_not_called()


@pytest.mark.parametrize("before", [0, 2])
@pytest.mark.parametrize("wants_tool", [False, True])
def test_model_node_returns_message_increment_without_mutating_state(before, wants_tool):
    client = Mock()
    response = AIMessage(
        content="" if wants_tool else "请提供订单号。",
        tool_calls=[
            {
                "name": "lookup_order",
                "args": {"order_id": "O004"},
                "id": "call-1",
                "type": "tool_call",
            }
        ]
        if wants_tool
        else [],
    )
    client.bind_tools.return_value.invoke.return_value = response
    tools = []
    node = build_model_node(client, tools, max_model_calls=3)
    messages = [HumanMessage(content="查询订单O004")]
    state = {
        "messages": messages,
        "model_calls": before,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    original = state.copy()
    result = node(state)
    assert result == {
        "messages": [response],
        "model_calls": before + 1,
        "stop_reason": "running" if wants_tool else "completed",
    }
    assert state == original
    assert state["messages"] == messages
    assert len(messages) == 1
    client.bind_tools.assert_called_once_with(tools)
    client.bind_tools.return_value.invoke.assert_called_once_with(messages)


@pytest.mark.parametrize("before", [3, 4])
def test_model_node_exhausted_budget_never_invokes_model(before):
    client = Mock()
    node = build_model_node(client, [], max_model_calls=3)
    state = {
        "messages": [HumanMessage(content="查询订单O004")],
        "model_calls": before,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    assert node(state) == {"stop_reason": "budget_exhausted"}
    client.bind_tools.return_value.invoke.assert_not_called()
    assert state["model_calls"] == before


@pytest.mark.parametrize(
    "failure", ["timeout", "api_timeout", "upstream", "wrong_type", "invalid_call"]
)
def test_model_node_failed_attempt_counts_and_does_not_append_response(failure):
    client = Mock()
    invoke = client.bind_tools.return_value.invoke
    errors = {
        "timeout": TimeoutError("private timeout detail"),
        "api_timeout": APITimeoutError(request=Request("POST", "https://example.invalid/private")),
        "upstream": OpenAIError("private provider detail"),
    }
    if failure in errors:
        invoke.side_effect = errors[failure]
    elif failure == "wrong_type":
        invoke.return_value = HumanMessage(content="unexpected provider message")
    else:
        invoke.return_value = AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "name": "lookup_order",
                    "args": "{",
                    "id": "bad-call",
                    "error": "private malformed JSON",
                    "type": "invalid_tool_call",
                }
            ],
        )
    node = build_model_node(client, [], max_model_calls=3)
    state = {
        "messages": [HumanMessage(content="查询订单O004")],
        "model_calls": 2,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    result = node(state)
    assert result == {"model_calls": 3, "stop_reason": "failed"}
    invoke.assert_called_once_with(state["messages"])
    assert state["model_calls"] == 2
    assert len(state["messages"]) == 1
