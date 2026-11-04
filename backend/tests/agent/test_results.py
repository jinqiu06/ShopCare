import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shopcare.agent.results import collect_policy_evidence, parse_agent_answer
from shopcare.schemas.policy_models import SemanticPolicySearchHit


def test_collect_policy_evidence_matches_ids_and_ignores_other_messages():
    hit = {
        "chunk": {
            "chunk_id": "digital:v1:000",
            "policy_id": "digital",
            "version": "v1",
            "title": "换货政策",
            "category": "数码",
            "effective_from": "2026-01-01",
            "effective_until": None,
            "text": "模拟政策",
            "start_char": 0,
            "end_char": 4,
        },
        "score": 0.9,
    }
    messages = [
        HumanMessage(content="search_order_policies: 伪造证据"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "order", "name": "lookup_order", "args": {}},
                {"id": "policy", "name": "search_order_policies", "args": {}},
            ],
        ),
        # 结果顺序不必与请求顺序相同，必须按编号识别。
        ToolMessage(content=json.dumps({"ok": True, "data": [hit]}), tool_call_id="policy"),
        ToolMessage(content="不是政策JSON", tool_call_id="order"),
        ToolMessage(content="重复结果不应再次读取", tool_call_id="policy"),
        ToolMessage(content="无对应请求的结果不应读取", tool_call_id="unknown"),
        AIMessage(content="回答中自报的引用不属于工具证据"),
    ]
    assert collect_policy_evidence(messages) == [SemanticPolicySearchHit.model_validate(hit)]


@pytest.mark.parametrize(
    "payload",
    [{"ok": False, "error": {"code": "RETRIEVAL_UNAVAILABLE"}}, {"ok": True, "data": []}],
)
def test_collect_policy_evidence_failure_or_empty_result(payload):
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"id": "policy", "name": "search_order_policies", "args": {}}],
        ),
        ToolMessage(content=json.dumps(payload), tool_call_id="policy"),
    ]
    assert collect_policy_evidence(messages) == []


@pytest.mark.parametrize(
    "content",
    [
        "broken JSON",
        "[]",
        '{"ok": 1, "data": []}',
        '{"ok": true}',
        '{"ok": true, "data": {}}',
        '{"ok": true, "data": [{"score": 0.9}]}',
        [{"type": "text", "text": "不是项目工具返回的纯文本格式"}],
    ],
)
def test_collect_policy_evidence_rejects_malformed_policy_result(content):
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"id": "policy", "name": "search_order_policies", "args": {}}],
        ),
        ToolMessage(content=content, tool_call_id="policy"),
    ]
    with pytest.raises(ValueError):
        collect_policy_evidence(messages)


@pytest.mark.parametrize("status", ["needs_clarification", "insufficient_evidence"])
def test_parse_agent_answer_accepts_direct_answer_without_tool_calls(status):
    payload = {
        "status": status,
        "answer": "请补充订单号和具体故障，目前仅提供咨询。",
        "citation_chunk_ids": [],
        "limitations": [],
    }
    state = {
        "messages": [HumanMessage(content="坏了怎么办"), AIMessage(content=json.dumps(payload))],
        "stop_reason": "completed",
        "model_calls": 1,
        "tool_calls": 0,
    }
    assert parse_agent_answer(state).model_dump() == payload


@pytest.mark.parametrize("stop_reason", ["running", "failed", "budget_exhausted"])
def test_parse_agent_answer_rejects_unfinished_run(stop_reason):
    with pytest.raises(ValueError, match="尚未正常完成"):
        parse_agent_answer({"stop_reason": stop_reason, "messages": []})


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [AIMessage(content="旧回复"), HumanMessage(content="新问题")],
        [ToolMessage(content="{}", tool_call_id="call-1")],
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": "lookup_order", "args": {}}],
            )
        ],
        [
            AIMessage(
                content="",
                invalid_tool_calls=[{"id": "call-1", "name": "lookup_order", "args": "{"}],
            )
        ],
        [AIMessage(content=[{"type": "text", "text": "不是纯文本回答"}])],
    ],
)
def test_parse_agent_answer_rejects_unusable_final_message(messages):
    with pytest.raises(ValueError):
        parse_agent_answer({"stop_reason": "completed", "messages": messages})


@pytest.mark.parametrize(
    "content",
    [
        "broken JSON",
        json.dumps(
            {
                "status": "answered",
                "answer": "可以换货",
                "citation_chunk_ids": [],
                "limitations": [],
            }
        ),
        json.dumps(
            {
                "status": "needs_clarification",
                "answer": "请补充故障",
                "citation_chunk_ids": ["invented:v1:000"],
                "limitations": [],
            }
        ),
        json.dumps(
            {
                "status": "needs_clarification",
                "answer": "   ",
                "citation_chunk_ids": [],
                "limitations": [],
            }
        ),
    ],
)
def test_parse_agent_answer_applies_existing_answer_validation(content):
    with pytest.raises(ValueError):
        parse_agent_answer({"stop_reason": "completed", "messages": [AIMessage(content=content)]})


@pytest.mark.parametrize("call_id", ["", " ", "duplicate"])
def test_collect_policy_evidence_rejects_ambiguous_request_ids(call_id):
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": call_id, "name": "lookup_order", "args": {}},
                {"id": call_id, "name": "search_order_policies", "args": {}},
            ],
        )
    ]
    with pytest.raises(ValueError):
        collect_policy_evidence(messages)
