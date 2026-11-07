from datetime import date

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shopcare.agent.graph import build_agent_graph
from shopcare.agent.prompts import build_agent_messages
from shopcare.agent.results import parse_agent_answer
from shopcare.agent.state import AgentState
from shopcare.schemas.answers import PolicyAnswer
from shopcare.services.conversations import (
    finish_conversation_turn,
    start_conversation_turn,
)
from shopcare.tools.orders import build_order_tools
from shopcare.tools.policy import build_policy_tools


def prepare_conversation_turn(
    engine: Engine,
    user_id: str,
    conversation_id: str,
) -> tuple[str, date]:
    with Session(engine) as session:
        with session.begin():
            conversation = start_conversation_turn(
                session,
                user_id,
                conversation_id,
            )

            thread_id = conversation.thread_id
            as_of = conversation.as_of
    return (thread_id, as_of)


def execute_conversation_graph(
    session: Session,
    chat_client: ChatOpenAI,
    embedding_client: OpenAIEmbeddings,
    checkpointer: BaseCheckpointSaver,
    user_id: str,
    thread_id: str,
    as_of: date,
    query: str,
) -> PolicyAnswer:
    """使用可信会话上下文执行图；资源和会话状态由外层管理。"""
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise ValueError("多轮执行需要有效的状态保存器")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("内部线程编号不能为空")
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > 1000:
        raise ValueError("问题必须为1至1000个字符")
    query = query.strip()

    tools = build_order_tools(session, user_id, as_of) + build_policy_tools(
        session,
        embedding_client,
        user_id,
        as_of,
    )
    graph = build_agent_graph(
        chat_client,
        tools,
        checkpointer=checkpointer,
        validate_answers=True,
    )
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if snapshot.next:
        raise ValueError("上轮执行未正常结束")

    history = snapshot.values.get("messages", [])
    if snapshot.values:
        if not isinstance(history, list) or not history:
            raise ValueError("已保存的会话历史不完整")

        # 上一轮也只允许引用该轮自己的证据，不能借更早的结果通过校验。
        last_user_index = None
        for index, message in enumerate(history):
            if isinstance(message, HumanMessage):
                last_user_index = index
        if last_user_index is None:
            raise ValueError("已保存的会话缺少用户消息")
        previous_turn = snapshot.values.copy()
        previous_turn["messages"] = history[last_user_index:]
        parse_agent_answer(previous_turn)

    history_count = len(history)
    messages = [HumanMessage(content=query)] if history else build_agent_messages(query, as_of)
    state: AgentState = {
        "messages": messages,
        "model_calls": 0,
        "tool_calls": 0,
        "stop_reason": "running",
    }
    result = graph.invoke(state, config=config)

    # 历史供模型理解上下文；引用校验只读取本轮新增的消息。
    current_turn = result.copy()
    current_turn["messages"] = result["messages"][history_count:]
    return parse_agent_answer(current_turn)


def run_conversation_turn(
    engine: Engine,
    chat_client: ChatOpenAI,
    embedding_client: OpenAIEmbeddings,
    checkpointer: BaseCheckpointSaver,
    user_id: str,
    conversation_id: str,
    query: str,
) -> PolicyAnswer:
    """执行已授权会话的一轮；开始/结束事务与图查询Session分开。"""
    if not isinstance(checkpointer, BaseCheckpointSaver):
        raise ValueError("多轮执行需要有效的状态保存器")
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > 1000:
        raise ValueError("问题必须为1至1000个字符")
    query = query.strip()

    thread_id, as_of = prepare_conversation_turn(
        engine,
        user_id,
        conversation_id,
    )
    try:
        with Session(engine) as session:
            answer = execute_conversation_graph(
                session,
                chat_client,
                embedding_client,
                checkpointer,
                user_id,
                thread_id,
                as_of,
                query,
            )
    except Exception as error:
        try:
            with Session(engine) as session:
                with session.begin():
                    finish_conversation_turn(
                        session,
                        user_id,
                        conversation_id,
                        succeeded=False,
                    )
        except Exception:
            error.add_note("会话失败状态未能写入,可能仍为running;请检查数据库后人工处理。")
        raise

    with Session(engine) as session:
        with session.begin():
            finish_conversation_turn(
                session,
                user_id,
                conversation_id,
                succeeded=True,
            )

    return answer
