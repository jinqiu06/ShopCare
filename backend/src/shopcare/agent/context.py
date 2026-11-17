from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from shopcare.services.memory import is_memory_command


class ContextLimitError(ValueError):
    """The mandatory current turn cannot fit without dropping evidence."""


def build_model_context(
    messages: list[BaseMessage],
    *,
    preference_context: str | None = None,
    reminder: SystemMessage | None = None,
    max_chars: int = 32000,
    max_previous_turns: int = 2,
) -> list[BaseMessage]:
    def size(entries: list[BaseMessage]) -> int:
        return sum(len(entry.model_dump_json()) for entry in entries)

    starts = [i for i, entry in enumerate(messages) if isinstance(entry, HumanMessage)]
    if not starts:
        raise ValueError("模型上下文缺少当前用户消息")
    prefix = list(messages[: starts[0]])
    if preference_context is not None:
        prefix.append(SystemMessage(content=preference_context))
    suffix = [reminder] if reminder is not None else []
    current = list(messages[starts[-1] :])
    if size(prefix + current + suffix) > max_chars:
        raise ContextLimitError("当前轮上下文超过模型请求长度上限，请新建会话并缩短问题")
    selected = []
    kept = 0
    for left, right in reversed(list(zip(starts[:-1], starts[1:]))):
        if is_memory_command(messages[left].content):
            continue
        if kept >= max_previous_turns:
            break
        turn = list(messages[left:right])
        if size(prefix + turn + selected + current + suffix) > max_chars:
            break
        selected = turn + selected
        kept += 1
    return prefix + selected + current + suffix
