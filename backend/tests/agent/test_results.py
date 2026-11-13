import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shopcare.agent.prompts import build_turn_reminder
from shopcare.agent.results import (
    collect_policy_evidence,
    parse_agent_answer,
    parse_agent_policy_answer,
    validate_consultation_answer,
    validate_policy_quotes,
)
from shopcare.schemas.answers import PolicyAnswer, PolicyQuote
from shopcare.schemas.policy_models import SemanticPolicySearchHit


@pytest.fixture
def quote_evidence():
    return [
        SemanticPolicySearchHit.model_validate(
            {
                "chunk": {
                    "chunk_id": chunk_id,
                    "policy_id": "demo",
                    "version": "v1",
                    "title": "模拟政策",
                    "category": "数码",
                    "effective_from": "2026-01-01",
                    "effective_until": None,
                    "text": text,
                    "start_char": 0,
                    "end_char": len(text),
                },
                "score": 0.9,
            }
        )
        for chunk_id, text in [
            ("exchange", "签收后15日内出现质量问题。正式提交需要用户确认。"),
            ("return", "商品未拆封时可以进一步核实退货条件。"),
        ]
    ]


@pytest.mark.parametrize(
    "quotes",
    [
        [],
        [("exchange", "正式提交需要用户确认。")],
        [("exchange", "签收后15日内"), ("exchange", "正式提交需要用户确认。")],
        [("exchange", "正式提交需要用户确认。"), ("return", "商品未拆封")],
    ],
)
def test_validate_policy_quotes_accepts_exact_excerpts(quote_evidence, quotes):
    items = [PolicyQuote(chunk_id=chunk_id, quote=text) for chunk_id, text in quotes]
    before = [hit.model_dump() for hit in quote_evidence]
    assert validate_policy_quotes(items, quote_evidence) is None
    assert [hit.model_dump() for hit in quote_evidence] == before


@pytest.mark.parametrize(
    "chunk_id,text,error",
    [
        ("old-policy", "正式提交需要用户确认。", "不属于本轮"),
        ("exchange", "正式提交需要平台审核。", "原文不匹配"),
        ("exchange", "商品未拆封", "原文不匹配"),
        ("exchange", "需要确认", "原文不匹配"),
    ],
)
def test_validate_policy_quotes_rejects_unknown_or_changed_excerpt(
    quote_evidence, chunk_id, text, error
):
    with pytest.raises(ValueError, match=error):
        validate_policy_quotes([PolicyQuote(chunk_id=chunk_id, quote=text)], quote_evidence)


def test_validate_policy_quotes_requires_evidence_for_nonempty_quotes():
    with pytest.raises(ValueError, match="不属于本轮"):
        validate_policy_quotes([PolicyQuote(chunk_id="exchange", quote="原文")], [])


@pytest.mark.parametrize(
    "status,quotes",
    [
        ("answered", ["正式提交需要用户确认。"]),
        ("answered", ["签收后15日内", "正式提交需要用户确认。"]),
        ("needs_clarification", []),
        ("insufficient_evidence", []),
    ],
)
def test_parse_agent_policy_answer_returns_public_format(quote_evidence, status, quotes):
    result = parse_agent_policy_answer(
        json.dumps(
            {
                "status": status,
                "answer": "仅提供政策咨询，故障情况仍需核实。",
                "citation_chunk_ids": ["exchange"] if quotes else [],
                "limitations": [],
                "policy_quotes": [{"chunk_id": "exchange", "quote": text} for text in quotes],
            },
            ensure_ascii=False,
        ),
        quote_evidence,
    )
    assert type(result) is PolicyAnswer
    assert result.status == status
    assert set(result.model_dump()) == {"status", "answer", "citation_chunk_ids", "limitations"}


@pytest.mark.parametrize(
    "ids,quotes,error",
    [
        (["exchange"], [], "编号不一致"),
        ([], [("exchange", "正式提交需要用户确认。")], "编号不一致"),
        (
            ["exchange"],
            [("exchange", "正式提交需要用户确认。"), ("return", "商品未拆封")],
            "编号不一致",
        ),
        (["exchange"], [("exchange", "正式提交需要平台审核。")], "原文不匹配"),
        (
            ["exchange", "exchange"],
            [("exchange", "正式提交需要用户确认。")],
            "重复ID",
        ),
        ([], [], "必须提供引用"),
    ],
)
def test_parse_agent_policy_answer_rejects_invalid_quote_coverage(
    quote_evidence, ids, quotes, error
):
    content = json.dumps(
        {
            "status": "answered",
            "answer": "这是模拟政策咨询。",
            "citation_chunk_ids": ids,
            "limitations": [],
            "policy_quotes": [{"chunk_id": chunk_id, "quote": text} for chunk_id, text in quotes],
        },
        ensure_ascii=False,
    )
    with pytest.raises(ValueError, match=error):
        parse_agent_policy_answer(content, quote_evidence)


@pytest.mark.parametrize("field", ["answer", "limitations"])
@pytest.mark.parametrize(
    "claim",
    [
        "我们将为您生成换货申请草稿。",
        "我们可以创建换货申请。",
        "已为您提交退款申请。",
        "我们会为您换货。",
        "正在为您办理售后。",
    ],
)
def test_consultation_boundary_rejects_execution_promises(field, claim):
    answer = PolicyAnswer(
        status="needs_clarification",
        answer=claim if field == "answer" else "请补充故障。",
        citation_chunk_ids=[],
        limitations=[claim] if field == "limitations" else [],
    )
    with pytest.raises(ValueError, match="不能承诺"):
        validate_consultation_answer(answer)
    with pytest.raises(ValueError, match="不能承诺"):
        parse_agent_answer(
            {
                "messages": [
                    AIMessage(content=json.dumps({**answer.model_dump(), "policy_quotes": []}))
                ],
                "stop_reason": "completed",
            }
        )


@pytest.mark.parametrize(
    "text",
    [
        "本次仅提供政策咨询，尚未创建任何申请。",
        "我们不能为您生成换货申请草稿。",
        "政策中提到生成换货草稿，本系统不具备该能力。",
        "生成草稿不代表已经创建申请。",
        "并非我们已经为您提交换货申请。",
        "没有本轮证据，不能认定符合资格。",
    ],
)
def test_consultation_boundary_allows_negation_and_policy_description(text):
    validate_consultation_answer(
        PolicyAnswer(
            status="needs_clarification", answer=text, citation_chunk_ids=[], limitations=[]
        )
    )


def test_turn_reminder_excludes_old_evidence_and_does_not_modify_history():
    hit = {
        "chunk": {
            "chunk_id": "old-policy",
            "policy_id": "digital",
            "version": "v1",
            "title": "换货",
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
        HumanMessage(content="上轮"),
        AIMessage(
            content="",
            tool_calls=[{"id": "old", "name": "search_order_policies", "args": {}}],
        ),
        ToolMessage(content=json.dumps({"ok": True, "data": [hit]}), tool_call_id="old"),
    ]
    assert '片段ID为:["old-policy"]' in build_turn_reminder(messages).content.replace("：", ":")
    messages.append(HumanMessage(content="左边没声音"))
    original = list(messages)
    reminder = build_turn_reminder(messages)
    assert "片段ID为:[]" in reminder.content.replace("：", ":")
    assert "不能生成草稿" in reminder.content
    assert messages == original


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
        "policy_quotes": [],
    }
    state = {
        "messages": [HumanMessage(content="坏了怎么办"), AIMessage(content=json.dumps(payload))],
        "stop_reason": "completed",
        "model_calls": 1,
        "tool_calls": 0,
    }
    assert parse_agent_answer(state).model_dump() == {
        key: value for key, value in payload.items() if key != "policy_quotes"
    }


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
                "policy_quotes": [],
            }
        ),
        json.dumps(
            {
                "status": "needs_clarification",
                "answer": "请补充故障",
                "citation_chunk_ids": ["invented:v1:000"],
                "limitations": [],
                "policy_quotes": [{"chunk_id": "invented:v1:000", "quote": "原文"}],
            }
        ),
        json.dumps(
            {
                "status": "needs_clarification",
                "answer": "   ",
                "citation_chunk_ids": [],
                "limitations": [],
                "policy_quotes": [],
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
