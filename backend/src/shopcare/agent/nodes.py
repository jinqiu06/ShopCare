import json
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import OpenAIError

from shopcare.agent.prompts import build_turn_reminder
from shopcare.agent.results import collect_policy_evidence, parse_agent_answer
from shopcare.agent.state import AgentState
from shopcare.schemas.answers import PolicyAnswer
from shopcare.tools.executor import execute_tool


def build_model_node(
    chat_client: ChatOpenAI,
    tools: list[BaseTool],
    max_model_calls: int = 3,
    validate_answers: bool = False,
    propagate_model_errors: bool = False,
):
    model = chat_client.bind_tools(tools)

    def call_model(state: AgentState) -> dict:
        if state["model_calls"] >= max_model_calls:
            return {"stop_reason": "budget_exhausted"}
        next_calls = state["model_calls"]
        updates = []
        require_policy = False
        while next_calls < max_model_calls:
            messages = [*state["messages"], *updates]
            request = [*messages, build_turn_reminder(messages)] if validate_answers else messages
            next_calls += 1
            try:
                active_model = (
                    chat_client.bind_tools(tools, tool_choice="search_order_policies")
                    if require_policy
                    else model
                )
                response = active_model.invoke(request)
            except (OpenAIError, TimeoutError):
                if propagate_model_errors:
                    # Application entry points must receive the original provider error.
                    raise
                return {
                    **({"messages": updates} if updates else {}),
                    "model_calls": next_calls,
                    "stop_reason": "failed",
                }

            if not isinstance(response, AIMessage) or response.invalid_tool_calls:
                return {
                    **({"messages": updates} if updates else {}),
                    "model_calls": next_calls,
                    "stop_reason": "failed",
                }
            if validate_answers:
                # 每次尝试单独记录，避免提供方重复ID让消息合并器覆盖先前候选。
                response = response.model_copy(update={"id": str(uuid4())})
            updates.append(response)
            if validate_answers and not response.tool_calls:
                start = 0
                for index, message in enumerate(messages):
                    if isinstance(message, HumanMessage):
                        start = index
                try:
                    parse_agent_answer(
                        {
                            **state,
                            "messages": [*messages[start:], response],
                            "stop_reason": "completed",
                        }
                    )
                except ValueError:
                    if next_calls < max_model_calls:
                        try:
                            payload = json.loads(response.content)
                            # 仅用于决定是否要求检索；不能代替上面的严格五字段校验。
                            candidate = (
                                PolicyAnswer.model_validate(
                                    {
                                        key: value
                                        for key, value in payload.items()
                                        if key != "policy_quotes"
                                    }
                                )
                                if isinstance(payload, dict)
                                else None
                            )
                        except (ValueError, TypeError):
                            candidate = None
                        require_policy = (
                            candidate is not None
                            and (
                                candidate.status == "answered" or bool(candidate.citation_chunk_ids)
                            )
                            and not collect_policy_evidence(messages[start:])
                            and any(entry.name == "search_order_policies" for entry in tools)
                        )
                        updates.append(
                            SystemMessage(
                                content="刚才的候选回答未通过格式、引用、原文摘录或执行承诺校验，未交付用户。"
                                "请纠正：政策说明必须先检索本轮证据，不能沿用历史引用；"
                                "本系统不能办理售后，不得承诺生成草稿/申请，也不能声称已执行。"
                                "仅依据本轮订单事实和政策进行条件性解释。",
                                # 下次请求的临时提醒会再次说明五字段及摘录规则。
                            )
                        )
                        continue
                    # 保留最后一次候选，由外层同一解析器拒绝；不增加预算。
            return {
                "messages": updates,
                "model_calls": next_calls,
                "stop_reason": "running" if response.tool_calls else "completed",
            }

    return call_model


def build_tool_node(
    tools: dict[str, BaseTool],
    max_tool_calls: int = 4,
    max_output_chars: int = 12000,
    max_model_calls: int | None = None,
):
    def call_tools(state: AgentState) -> dict:
        # 读取最后一条模型消息，执行请求并返回消息增量
        if not state["messages"]:
            return {"stop_reason": "failed"}

        message = state["messages"][-1]

        if not isinstance(message, AIMessage) or len(message.tool_calls) == 0:
            return {"stop_reason": "failed"}
        if max_model_calls is not None and state["model_calls"] >= max_model_calls:
            return {"stop_reason": "budget_exhausted"}
        call_ids = [tool_call.get("id") for tool_call in message.tool_calls]

        if any(not isinstance(call_id, str) or not call_id.strip() for call_id in call_ids) or len(
            call_ids
        ) != len(set(call_ids)):
            return {"stop_reason": "failed"}
        if state["tool_calls"] + len(message.tool_calls) > max_tool_calls:
            return {"stop_reason": "budget_exhausted"}
        results = []
        update = {
            "messages": results,
            "tool_calls": state["tool_calls"],
            "stop_reason": "running",
        }

        for tool_call in message.tool_calls:
            update["tool_calls"] += 1

            try:
                result = execute_tool(tools, tool_call["name"], tool_call["args"])
                if not isinstance(result, dict):
                    raise TypeError("工具结果必须为字典")
                content = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except Exception:
                # End this run; never reuse a possibly failed transaction or expose its details.
                update["stop_reason"] = "failed"
                return update

            if len(content) > max_output_chars:
                update["stop_reason"] = "failed"
                return update

            results.append(
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call["id"],
                )
            )

            error = result.get("error")
            if isinstance(error, dict) and error.get("code") in {
                "TEMPORARILY_UNAVAILABLE",
                "RETRIEVAL_UNAVAILABLE",
            }:
                update["stop_reason"] = "failed"
                return update

        return update

    return call_tools
