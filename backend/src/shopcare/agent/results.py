import json
import re

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from shopcare.agent.state import AgentState
from shopcare.schemas.answers import AgentPolicyAnswer, PolicyAnswer, PolicyQuote
from shopcare.schemas.policy_models import SemanticPolicySearchHit
from shopcare.services.answer_generation import parse_policy_answer


def validate_consultation_answer(answer: PolicyAnswer) -> None:
    """拦截明确的执行承诺；不是完整的业务语义判定器。"""
    action = (
        r"(?:生成|创建|提交|发起|申请|办理|执行|处理)"
        r"[^。！？；\n]{0,16}(?:草稿|申请|退款|换货|售后)"
    )
    promise = re.compile(
        rf"(?:我们|我|本系统|系统|平台)(?:现在|马上|立即)?"
        rf"(?:将|会|可以|可|能|已经|已|正在)(?:直接|立即|马上)?"
        rf"(?:为您|为你|帮您|帮你|替您|替你)?(?:{action}|退款|换货)"
        rf"|(?:已经|已|正在)(?:为您|为你|帮您|帮你)?(?:{action}|退款|换货)"
    )
    for text in [answer.answer, *answer.limitations]:
        for match in promise.finditer(text):
            # “不代表已经创建申请”等否定说明不属于执行承诺。
            prefix = text[: match.start()]
            if re.search(
                r"(?:不代表|不表示|并非|并未|没有|不会|不能|无法|不承诺|不保证)"
                r"[^。！？；，,\n]{0,8}$",
                prefix,
            ):
                continue
            raise ValueError("咨询回答不能承诺或声称执行售后操作")


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
    answer = parse_agent_policy_answer(message.content, evidence)
    validate_consultation_answer(answer)
    return answer


def validate_policy_quotes(
    quotes: list[PolicyQuote],
    evidence: list[SemanticPolicySearchHit],
) -> None:
    texts = {hit.chunk.chunk_id: hit.chunk.text for hit in evidence}
    for quote in quotes:
        if quote.chunk_id not in texts:
            raise ValueError("摘录不属于本轮检索证据")
        if quote.quote not in texts[quote.chunk_id]:
            raise ValueError("摘录与政策原文不匹配")


def parse_agent_policy_answer(
    content: str,
    evidence: list[SemanticPolicySearchHit],
) -> PolicyAnswer:
    candidate = AgentPolicyAnswer.model_validate_json(content)

    validate_policy_quotes(candidate.policy_quotes, evidence)

    quoted_ids = {quote.chunk_id for quote in candidate.policy_quotes}
    if quoted_ids != set(candidate.citation_chunk_ids):
        raise ValueError("引用编号与摘录编号不一致")

    public_content = candidate.model_dump_json(
        exclude={"policy_quotes"},
    )
    return parse_policy_answer(public_content, evidence)
