import json
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shopcare.schemas.after_sales import (
    AfterSalesApplicationResponse,
    AfterSalesConfirmRequest,
    AfterSalesDraftResponse,
    AfterSalesFacts,
)
from shopcare.schemas.answers import AgentPolicyAnswer, PolicyAnswer
from shopcare.services.after_sales import (
    DraftConflictError,
    SubmissionNotEligibleError,
    cancel_after_sales_draft,
    confirm_after_sales_draft,
    get_after_sales_application_for_user,
    get_after_sales_draft_for_user,
)

ACTION_NAMES = {
    "prepare_after_sales_draft",
    "confirm_after_sales_draft",
    "cancel_after_sales_draft",
    "view_after_sales_draft",
}


def build_after_sales_answer(name: str, result: dict) -> PolicyAnswer:
    if not result["ok"]:
        return PolicyAnswer(
            status="needs_clarification",
            answer=result["error"]["message"],
            citation_chunk_ids=[],
            limitations=[],
        )
    data = result["data"]
    draft = AfterSalesDraftResponse.model_validate(data["draft"])
    application = data.get("application")
    if application is not None:
        application = AfterSalesApplicationResponse.model_validate(application)
        return PolicyAnswer(
            status="answered",
            answer=f"模拟申请编号：{application.id}。对应草稿：{draft.id}，状态：待处理。",
            citation_chunk_ids=[],
            limitations=["仅已登记模拟申请，不代表质量核实、资格审核通过或退款/换货完成。"],
        )
    if draft.status == "cancelled":
        return PolicyAnswer(
            status="needs_clarification",
            answer=f"草稿 {draft.id} 已取消，未提交申请。",
            citation_chunk_ids=[],
            limitations=[],
        )
    if data.get("expired") is True:
        return PolicyAnswer(
            status="needs_clarification",
            answer=f"草稿 {draft.id} 已过期，未提交申请。如需继续，请重新申请售后。",
            citation_chunk_ids=[],
            limitations=[],
        )
    facts = AfterSalesFacts.model_validate(data["facts"])
    labels = {
        "issue_type": "问题类型",
        "unopened": "未拆封",
        "unused": "无使用痕迹",
        "accessories_complete": "配件齐全",
        "packaging_complete": "包装齐全",
        "malfunction": "功能故障",
        "damage": "破损",
        "wrong_spec": "规格不符",
    }
    declarations = "；".join(
        f"{labels[key]}：{('是' if value else '否') if isinstance(value, bool) else labels[value]}"
        for key, value in facts.model_dump(exclude_none=True).items()
    )
    request_type = "退货" if draft.request_type == "return" else "换货"
    return PolicyAnswer(
        status="needs_clarification",
        answer=(
            f"待确认草稿：{draft.id}，版本 {draft.version}。\n"
            f"订单：{draft.order_id}；商品项：{draft.item_id}；诉求：{request_type}；原因：{draft.reason}。\n"
            f"草稿有效期至 {draft.expires_at.isoformat()} UTC。\n"
            f"请核对用户自述：{declarations}。\n"
            f"内容无误后发送完整指令：确认提交 {draft.id} 版本 {draft.version}\n"
            f"取消可发送：取消草稿 {draft.id} 版本 {draft.version}"
        ),
        citation_chunk_ids=[],
        limitations=["草稿尚未提交，以上商品状态为用户自述，未经检测。"],
    )


def build_action_messages(name: str, result: dict) -> list[BaseMessage]:
    call_id = str(uuid4())
    answer = build_after_sales_answer(name, result)
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id),
        AIMessage(
            content=AgentPolicyAnswer(**answer.model_dump(), policy_quotes=[]).model_dump_json()
        ),
    ]


def parse_after_sales_answer(messages: list[BaseMessage]) -> PolicyAnswer | None:
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    calls = {}
    for message in messages[start:]:
        if isinstance(message, AIMessage):
            calls.update({call["id"]: call["name"] for call in message.tool_calls})
        elif isinstance(message, ToolMessage):
            name = calls.pop(message.tool_call_id, None)
            if name not in ACTION_NAMES:
                continue
            result = json.loads(message.content)
            if name == "prepare_after_sales_draft" and not result["ok"]:
                continue
            expected = build_after_sales_answer(name, result)
            actual = AgentPolicyAnswer.model_validate_json(messages[-1].content)
            if actual != AgentPolicyAnswer(**expected.model_dump(), policy_quotes=[]):
                raise ValueError("售后操作必须展示本轮可信工具结果，不接受模型改写的执行结论")
            return expected
    return None


def run_after_sales_command(
    engine: Engine,
    user_id: str,
    query: str,
    pending: dict | None,
) -> tuple[str, dict] | None:
    if not query.startswith(("确认提交", "取消草稿", "查看售后")):
        return None
    name = (
        "confirm_after_sales_draft"
        if query.startswith("确认提交")
        else "cancel_after_sales_draft"
        if query.startswith("取消草稿")
        else "view_after_sales_draft"
    )
    if pending is None:
        return name, {
            "ok": False,
            "error": {"message": "当前会话没有待确认草稿，请先明确申请售后。"},
        }
    draft = AfterSalesDraftResponse.model_validate(pending["draft"])
    expected = (
        f"确认提交 {draft.id} 版本 {draft.version}"
        if name == "confirm_after_sales_draft"
        else f"取消草稿 {draft.id} 版本 {draft.version}"
        if name == "cancel_after_sales_draft"
        else "查看售后"
    )
    if query != expected:
        return name, {"ok": False, "error": {"message": f"请核对草稿后发送完整指令：{expected}"}}
    try:
        with Session(engine) as session, session.begin():
            current = get_after_sales_draft_for_user(session, user_id, draft.id)
            if current is None:
                raise LookupError("草稿不存在或不可访问")
            if name == "confirm_after_sales_draft":
                application = confirm_after_sales_draft(
                    session,
                    user_id,
                    draft.id,
                    AfterSalesConfirmRequest(
                        expected_version=draft.version,
                        confirmed=True,
                        facts=AfterSalesFacts.model_validate(pending["facts"]),
                    ),
                )
            elif name == "cancel_after_sales_draft":
                cancel_after_sales_draft(session, user_id, draft.id, draft.version)
                application = None
            else:
                application = get_after_sales_application_for_user(session, user_id, draft.id)
            data = {
                "draft": AfterSalesDraftResponse.model_validate(current).model_dump(mode="json"),
                "facts": pending["facts"],
                "application": (
                    AfterSalesApplicationResponse.model_validate(application).model_dump(
                        mode="json"
                    )
                    if application is not None
                    else None
                ),
            }
            if name == "view_after_sales_draft" and current.status == "pending":
                # Keep the result stable when validating this answer from a later checkpoint.
                data["expired"] = current.expires_at <= datetime.now(timezone.utc).replace(
                    tzinfo=None
                )
        return name, {"ok": True, "data": data}
    except LookupError:
        return name, {"ok": False, "error": {"message": "草稿不存在或不可访问。"}}
    except (DraftConflictError, SubmissionNotEligibleError) as error:
        return name, {"ok": False, "error": {"message": str(error) + "；可发送“查看售后”查询。"}}
