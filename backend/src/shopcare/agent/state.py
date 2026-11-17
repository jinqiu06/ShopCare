from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    model_calls: int
    tool_calls: int
    pending_after_sales: NotRequired[dict]
    stop_reason: Literal[
        "running",
        "completed",
        "budget_exhausted",
        "failed",
    ]
