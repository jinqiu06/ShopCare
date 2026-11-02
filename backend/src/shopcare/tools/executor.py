from langchain_core.tools import BaseTool
from pydantic import ValidationError


def execute_tool(
    tools: dict[str, BaseTool],
    tool_name: str,
    arguments: dict,
) -> dict:
    selected = tools.get(tool_name)
    if not selected:
        return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": "工具不存在"}}
    try:
        selected.get_input_schema().model_validate(arguments)
    except ValidationError:
        return {
            "ok": False,
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": "工具参数不合法",
            },
        }

    return selected.invoke(arguments)
