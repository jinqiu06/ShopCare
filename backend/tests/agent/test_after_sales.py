import sqlite3
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopcare.agent.after_sales import run_after_sales_command
from shopcare.agent.agent import run_conversation_turn
from shopcare.agent.checkpoints import initialize_agent_checkpoint
from shopcare.agent.results import parse_agent_answer
from shopcare.db import build_engine, init_db
from shopcare.db.models import (
    AfterSalesApplication,
    AfterSalesDraft,
    Conversation,
    PolicyEmbeddingRecord,
)
from shopcare.schemas.answers import AgentPolicyAnswer
from shopcare.scripts.seed import seed_demo_data
from shopcare.services.conversations import create_conversation
from shopcare.services.policies.chunker import split_policy
from shopcare.services.policies.loader import load_policies
from shopcare.services.policies.store import replace_policy_chunks


@pytest.fixture
def sales_context(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'sales.db').as_posix()}")
    init_db(engine)
    with Session(engine) as session, session.begin():
        seed_demo_data(session)
        for policy in load_policies(Path(__file__).resolve().parents[3] / "data/policies"):
            chunks = split_policy(policy)
            replace_policy_chunks(session, policy, chunks)
            for chunk in chunks:
                session.add(
                    PolicyEmbeddingRecord(
                        chunk_id=chunk.chunk_id,
                        model_id="test",
                        dimensions=2,
                        vector=[1.0, 0.0],
                    )
                )
        row = create_conversation(session, "U001", date(2026, 11, 2))
        ref = (row.id, row.thread_id)
    embedding = Mock()
    embedding.model, embedding.dimensions = "test", 2
    embedding.embed_query.return_value = [1.0, 0.0]
    try:
        yield engine, ref, str(tmp_path / "checkpoint.db"), embedding
    finally:
        engine.dispose()


def preparation(**changes):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "prepare-1",
                "name": "prepare_after_sales_draft",
                "args": {
                    "order_id": "O004",
                    "item_id": "O004-I01",
                    "request_type": "exchange",
                    "reason": "耳机单侧无声",
                    "facts": {"issue_type": "malfunction"},
                    **changes,
                },
            }
        ],
    )


def clarification():
    return AIMessage(
        content=AgentPolicyAnswer(
            status="needs_clarification",
            answer="请核对信息后明确申请或确认。",
            citation_chunk_ids=[],
            limitations=[],
            policy_quotes=[],
        ).model_dump_json()
    )


def prepare_turn(context, saver, *, with_retrieval=False):
    engine, (conversation_id, thread_id), _, embedding = context
    initialize_agent_checkpoint(saver, thread_id)
    chat = Mock()
    responses = []
    if with_retrieval:
        responses.append(
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "lookup", "name": "lookup_order", "args": {"order_id": "O004"}},
                    {
                        "id": "policies",
                        "name": "search_order_policies",
                        "args": {
                            "order_id": "O004",
                            "item_id": "O004-I01",
                            "query": "耳机单侧无声换货",
                        },
                    },
                ],
            )
        )
    responses.append(preparation())
    chat.bind_tools.return_value.invoke.side_effect = responses
    answer = run_conversation_turn(
        engine,
        chat,
        embedding,
        saver,
        "U001",
        conversation_id,
        "申请售后：订单O004商品O004-I01耳机单侧无声，我想换货。",
    )
    snapshot = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    state = snapshot.checkpoint["channel_values"]
    return answer, state["pending_after_sales"], chat, state


def test_retrieval_prepare_reopen_confirm_and_retry_use_actual_services(sales_context):
    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        answer, pending, chat, state = prepare_turn(sales_context, saver, with_retrieval=True)
        draft_id = pending["draft"]["id"]
        assert draft_id in answer.answer and "用户自述" in answer.answer
        assert {tool.name for tool in chat.bind_tools.call_args.args[0]} == {
            "lookup_order",
            "search_order_policies",
            "prepare_after_sales_draft",
        }
        assert chat.bind_tools.return_value.invoke.call_count == 2
        assert state["model_calls"] == 2 and state["tool_calls"] == 3
        assert state["stop_reason"] == "completed"
    engine.dispose()
    chat = Mock()
    command = f"确认提交 {draft_id} 版本 1"
    with SqliteSaver.from_conn_string(path) as saver:
        confirmed = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, command
        )
        assert confirmed.status == "answered" and "待处理" in confirmed.answer
        with Session(engine) as session, session.begin():
            session.get(AfterSalesDraft, draft_id).expires_at = datetime(2020, 1, 1)
        replay = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, command
        )
        assert replay == confirmed
        viewed = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, "查看售后"
        )
        assert viewed == confirmed
    chat.bind_tools.return_value.invoke.assert_not_called()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 1
        assert session.get(AfterSalesDraft, draft_id).version == 2
        assert session.get(Conversation, conversation_id).status == "ready"


def test_plain_ack_and_partial_confirmation_never_submit(sales_context):
    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        _, pending, _, _ = prepare_turn(sales_context, saver)
        chat = Mock()
        chat.bind_tools.return_value.invoke.return_value = clarification()
        for query in ("好", "确认提交", f"确认提交 {pending['draft']['id']} 版本 2"):
            answer = run_conversation_turn(
                engine, chat, embedding, saver, "U001", conversation_id, query
            )
            assert answer.status == "needs_clarification"
        assert chat.bind_tools.return_value.invoke.call_count == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0


def test_cancel_is_persisted_and_later_confirmation_is_rejected(sales_context):
    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        _, pending, _, _ = prepare_turn(sales_context, saver)
        draft_id = pending["draft"]["id"]
        chat = Mock()
        cancelled = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, f"取消草稿 {draft_id} 版本 1"
        )
        assert "已取消" in cancelled.answer
        answer = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, f"确认提交 {draft_id} 版本 1"
        )
        assert answer.status == "needs_clarification"
        viewed = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, "查看售后"
        )
        assert "已取消" in viewed.answer
    chat.bind_tools.return_value.invoke.assert_not_called()
    with Session(engine) as session:
        assert session.get(AfterSalesDraft, draft_id).status == "cancelled"
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0


@pytest.mark.parametrize("case", ["no_intent", "foreign_order", "missing_facts", "batched"])
def test_model_tool_request_cannot_bypass_intent_ownership_facts_or_batch_boundary(
    sales_context, case
):
    engine, (conversation_id, thread_id), path, embedding = sales_context
    changes = {}
    if case == "foreign_order":
        changes = {"order_id": "O014", "item_id": "O014-I01"}
    elif case == "missing_facts":
        changes = {"request_type": "return", "facts": {}}
    request = preparation(**changes)
    if case == "batched":
        request = request.model_copy(
            update={
                "tool_calls": [
                    *request.tool_calls,
                    {
                        "id": "lookup",
                        "name": "lookup_order",
                        "args": {"order_id": "O004"},
                    },
                ]
            }
        )
    chat = Mock()
    chat.bind_tools.return_value.invoke.side_effect = [request, clarification()]
    with SqliteSaver.from_conn_string(path) as saver:
        initialize_agent_checkpoint(saver, thread_id)
        answer = run_conversation_turn(
            engine,
            chat,
            embedding,
            saver,
            "U001",
            conversation_id,
            "好" if case == "no_intent" else "申请售后：请准备草稿",
        )
        assert answer.status == "needs_clarification"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesDraft)) == 0
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0
    embedding.embed_query.assert_not_called()


def test_other_conversation_and_other_user_cannot_confirm_pending_draft(sales_context):
    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        _, pending, _, _ = prepare_turn(sales_context, saver)
        command = f"确认提交 {pending['draft']['id']} 版本 1"
        with Session(engine) as session, session.begin():
            other = create_conversation(session, "U001", date(2026, 11, 2))
            other_id, other_thread = other.id, other.thread_id
        initialize_agent_checkpoint(saver, other_thread)
        answer = run_conversation_turn(engine, Mock(), embedding, saver, "U001", other_id, command)
        assert "没有待确认草稿" in answer.answer
        _, result = run_after_sales_command(engine, "U002", command, pending)
        assert result["ok"] is False
        assert "不可访问" in result["error"]["message"]
    with Session(engine) as session:
        assert session.get(Conversation, conversation_id).status == "ready"
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0


def test_pending_checkpoint_survives_history_window_and_model_claims_are_not_receipts(
    sales_context,
):
    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        _, pending, _, state = prepare_turn(sales_context, saver)
        start = next(i for i, m in enumerate(state["messages"]) if isinstance(m, HumanMessage))
        corrupt = {**state, "messages": list(state["messages"][start:])}
        corrupt["messages"][-1] = clarification()
        with pytest.raises(ValueError, match="可信工具结果"):
            parse_agent_answer(corrupt)
        chat = Mock()
        chat.bind_tools.return_value.invoke.return_value = clarification()
        for _ in range(4):
            run_conversation_turn(
                engine, chat, embedding, saver, "U001", conversation_id, "我再考虑一下"
            )
        answer = run_conversation_turn(
            engine, chat, embedding, saver, "U001", conversation_id, "查看售后"
        )
        assert pending["draft"]["id"] in answer.answer
        answer = run_conversation_turn(
            engine,
            chat,
            embedding,
            saver,
            "U001",
            conversation_id,
            f"确认提交 {pending['draft']['id']} 版本 1",
        )
        assert answer.status == "answered"


def test_confirmation_rechecks_changed_order_and_does_not_fail_conversation(sales_context):
    from shopcare.db.models import Order

    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        _, pending, _, _ = prepare_turn(sales_context, saver)
        with Session(engine) as session, session.begin():
            session.get(Order, "O004").status = "cancelled"
        answer = run_conversation_turn(
            engine,
            Mock(),
            embedding,
            saver,
            "U001",
            conversation_id,
            f"确认提交 {pending['draft']['id']} 版本 1",
        )
        assert "签收" in answer.answer
    with Session(engine) as session:
        assert session.get(Conversation, conversation_id).status == "ready"
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 0


def test_checkpoint_failure_after_confirm_preserves_receipt_and_marks_failed(
    sales_context,
    monkeypatch,
):
    engine, (conversation_id, _), path, embedding = sales_context
    with SqliteSaver.from_conn_string(path) as saver:
        _, pending, _, _ = prepare_turn(sales_context, saver)
        command = f"确认提交 {pending['draft']['id']} 版本 1"
        monkeypatch.setattr(
            saver, "put", Mock(side_effect=sqlite3.OperationalError("disk failure"))
        )
        with pytest.raises(sqlite3.OperationalError):
            run_conversation_turn(
                engine, Mock(), embedding, saver, "U001", conversation_id, command
            )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AfterSalesApplication)) == 1
        assert session.get(AfterSalesDraft, pending["draft"]["id"]).version == 2
        assert session.get(Conversation, conversation_id).status == "failed"
