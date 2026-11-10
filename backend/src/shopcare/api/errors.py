"""Consistent public HTTP error responses."""

from fastapi import HTTPException as APIHTTPException
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and {"code", "message"} <= exc.detail.keys():
        error = {"code": exc.detail["code"], "message": exc.detail["message"]}
    else:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        error = {"code": code, "message": str(exc.detail)}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo request input or validator context into public errors.
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数格式不正确",
            }
        },
    )


# Public response policy for expected conversation-message failures.
CONVERSATION_MESSAGE_ERRORS = {
    "CONVERSATION_NOT_FOUND": (404, "会话不存在或不可访问"),
    "CONVERSATION_NOT_READY": (409, "会话当前不可执行"),
    "CONVERSATION_CHECKPOINT_MISSING": (409, "会话历史不存在，无法继续，请新建会话"),
    "CONVERSATION_UNAVAILABLE": (503, "会话服务暂不可用，请稍后重试"),
    "AGENT_NOT_CONFIGURED": (503, "对话服务配置暂不可用"),
    "AGENT_TIMEOUT": (504, "对话服务超时，请稍后查询会话状态"),
    "AGENT_UPSTREAM_ERROR": (502, "对话服务暂不可用，请稍后查询会话状态"),
}


def conversation_message_http_error(code: str) -> APIHTTPException:
    status, message = CONVERSATION_MESSAGE_ERRORS[code]
    return APIHTTPException(status_code=status, detail={"code": code, "message": message})
