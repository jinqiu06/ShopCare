import json

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import OpenAIError

from shopcare.agent.state import AgentState
from shopcare.tools.executor import execute_tool


def build_model_node(
    chat_client: ChatOpenAI,
    tools: list[BaseTool],
    max_model_calls: int = 3,
):
    model = chat_client.bind_tools(tools)

    def call_model(state: AgentState) -> dict:
        if state["model_calls"] >= max_model_calls:
            return {"stop_reason": "budget_exhausted"}
        next_calls = state["model_calls"] + 1

        try:
            response = model.invoke(state["messages"])
        except (OpenAIError, TimeoutError):
            return {
                "model_calls": next_calls,
                "stop_reason": "failed",
            }

        if not isinstance(response, AIMessage) or response.invalid_tool_calls:
            return {
                "model_calls": next_calls,
                "stop_reason": "failed",
            }
        return {
            "messages": [response],
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
