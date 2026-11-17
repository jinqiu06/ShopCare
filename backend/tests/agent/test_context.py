from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from shopcare.agent.context import build_model_context
from shopcare.agent.nodes import build_model_node


def test_context_keeps_recent_whole_turns_and_current_tool_pairs_without_mutating_archive():
    current = [
        HumanMessage(content="current"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c1", "name": "lookup_order", "args": {"order_id": "O1"}},
            ],
        ),
        ToolMessage(content='{"ok":true}', tool_call_id="c1"),
    ]
    archive = [SystemMessage(content="rules")]
    for i in range(5):
        archive += [HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}")]
    archive += current
    before = [m.model_dump() for m in archive]
    result = build_model_context(archive)
    assert [m.content for m in result if isinstance(m, HumanMessage)] == ["q3", "q4", "current"]
    assert result[-3:] == current
    assert [m.model_dump() for m in archive] == before


def test_context_removes_old_memory_commands_and_counts_serialized_tool_arguments():
    history = [
        SystemMessage(content="rules"),
        HumanMessage(content="记住偏好：回答详细"),
        AIMessage(content="saved"),
        HumanMessage(content="new"),
    ]
    result = build_model_context(history, preference_context="未保存偏好")
    assert [m.content for m in result] == ["rules", "未保存偏好", "new"]
    history += [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c", "name": "lookup_order", "args": {"order_id": "x" * 1000}},
            ],
        )
    ]
    with pytest.raises(ValueError, match="上下文"):
        build_model_context(history, max_chars=800)


def test_context_drops_large_old_turn_whole_and_rejects_large_current_before_sdk():
    history = [
        SystemMessage(content="rules"),
        HumanMessage(content="x" * 2000),
        AIMessage(content="old"),
        HumanMessage(content="new"),
    ]
    result = build_model_context(history, max_chars=1000)
    assert [m.content for m in result] == ["rules", "new"]
    chat = Mock()
    with pytest.raises(ValueError, match="上下文"):
        build_model_node(chat, [])(
            {
                "messages": [HumanMessage(content="x" * 33000)],
                "model_calls": 0,
                "tool_calls": 0,
                "stop_reason": "running",
            }
        )
    chat.bind_tools.return_value.invoke.assert_not_called()
