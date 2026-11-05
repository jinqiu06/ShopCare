from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from shopcare.agent.nodes import build_model_node, build_tool_node
from shopcare.agent.state import AgentState


def route_after_model(state: AgentState) -> str:
    if state["stop_reason"] == "running":
        return "tools"
    else:
        return END


def route_after_tools(state: AgentState) -> str:
    if state["stop_reason"] == "running":
        return "model"
    else:
        return END


def build_agent_graph(
    chat_client: ChatOpenAI,
    tools: list[BaseTool],
    max_model_calls: int = 3,
    max_tool_calls: int = 4,
    max_output_chars: int = 12000,
    checkpointer: BaseCheckpointSaver | None = None,
):
    for name, value in (
        ("max_model_calls", max_model_calls),
        ("max_tool_calls", max_tool_calls),
        ("max_output_chars", max_output_chars),
    ):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} 必须是正整数")
    if any(not isinstance(entry, BaseTool) for entry in tools):
        raise ValueError("tools 必须由 BaseTool 实例组成")
    registry = {tool.name: tool for tool in tools}
    if len(registry) != len(tools):
        raise ValueError("工具名称不可重复")

    def validate_start(state: AgentState) -> dict:
        messages = state.get("messages", [])
        if (
            not messages
            or not all(isinstance(message, BaseMessage) for message in messages)
            or not isinstance(messages[-1], HumanMessage)
        ):
            raise ValueError("初始消息不能为空且必须以用户消息结束")
        for field in ("model_calls", "tool_calls"):
            if type(state.get(field)) is not int or state[field] != 0:
                raise ValueError("单次运行的初始调用次数必须为整数0")
        if state.get("stop_reason") != "running":
            raise ValueError("单次运行必须从running状态开始")
        return {}

    graph = StateGraph(AgentState)

    graph.add_node("validate_start", validate_start)
    graph.add_node(
        "model",
        build_model_node(chat_client, tools, max_model_calls),
    )
    graph.add_node(
        "tools",
        build_tool_node(
            registry, max_tool_calls, max_output_chars, max_model_calls=max_model_calls
        ),
    )

    graph.add_edge(START, "validate_start")
    graph.add_edge("validate_start", "model")
    graph.add_conditional_edges("model", route_after_model, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tools, {"model": "model", END: END})

    return graph.compile(
        checkpointer=checkpointer,
    ).with_config(
        recursion_limit=2 * max_model_calls + 4,
    )
