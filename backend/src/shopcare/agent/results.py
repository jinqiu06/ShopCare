import json

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from shopcare.agent.state import AgentState
from shopcare.schemas.answers import PolicyAnswer
from shopcare.schemas.policy_models import SemanticPolicySearchHit
from shopcare.services.answer_generation import parse_policy_answer


def collect_policy_evidence(
    messages: list[BaseMessage],
) -> list[SemanticPolicySearchHit]:
    """从本次可信运行的消息中提取成功政策工具返回的证据。"""
    pending_tools: dict[str, str] = {}
    evidence: list[SemanticPolicySearchHit] = []

    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                call_id = tool_call["id"]
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ValueError("工具调用编号不能为空")
                if call_id in pending_tools:
                    raise ValueError("尚未返回结果的工具调用编号不能重复")
                pending_tools[call_id] = tool_call["name"]

        elif isinstance(message, ToolMessage):
            tool_name = pending_tools.pop(message.tool_call_id, None)
            if tool_name != "search_order_policies":
                continue

            if not isinstance(message.content, str):
                raise ValueError("政策工具结果必须为JSON文本")
            result = json.loads(message.content)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError("政策工具结果必须包含布尔类型的ok字段")
            if result["ok"] is False:
                continue

            data = result.get("data")
            if not isinstance(data, list):
                raise ValueError("政策工具成功结果的data必须为列表")
            for item in data:
                evidence.append(SemanticPolicySearchHit.model_validate(item))

    return evidence


def parse_agent_answer(state: AgentState) -> PolicyAnswer:
    """将本次正常结束的可信Agent运行结果解析为带引用校验的回答。"""
    if state["stop_reason"] != "completed":
        raise ValueError("Agent尚未正常完成,不能提取最终回答")
    messages = state["messages"]
    if not messages:
        raise ValueError("消息列表不能为空")
    message = messages[-1]
    if not isinstance(message, AIMessage):
        raise ValueError("最后一条消息必须为模型回答")
    if message.tool_calls or message.invalid_tool_calls:
        raise ValueError("最终回答不能包含工具调用请求")
    if not isinstance(message.content, str):
        raise ValueError("结果必须为JSON文本")
    evidence = collect_policy_evidence(messages)
    return parse_policy_answer(message.content, evidence)
