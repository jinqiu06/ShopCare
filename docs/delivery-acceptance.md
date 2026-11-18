# ShopCare 交付演示与验收

2026-11-18 在 Windows、Python 3.13 上使用空白临时 SQLite 库验证。以下 Swagger 演示使用模拟订单和仓库自编政策，数据库和 HTTP 服务均在本机运行。

## 启动隔离演示环境

完成 [README 的环境安装](../README.md#环境与安装)后，在仓库根目录打开一个专门用于演示的 PowerShell。使用环境变量将演示库与 Checkpoint 指向本次新目录。配置模型 Key 是运行真实对话的前提，以下三个 Swagger 场景无需 Key。

```powershell
$shopcareDemo = Join-Path ([System.IO.Path]::GetTempPath()) ('shopcare-demo-' + [guid]::NewGuid())
$shopcareDemoSqlite = (Join-Path $shopcareDemo 'demo.db').Replace('\', '/')
$env:DATABASE_URL = "sqlite:///$shopcareDemoSqlite"
$env:AGENT_CHECKPOINT_DB = Join-Path $shopcareDemo 'checkpoints.db'
.venv/Scripts/python -m shopcare.scripts.seed
.venv/Scripts/python -m shopcare.scripts.ingest_policies
.venv/Scripts/python -m uvicorn shopcare.main:app --host 127.0.0.1 --port 8000
```

首次初始化应输出 2 位用户、20 笔订单和 40 个商品项，导入应输出 8 份政策、8 个片段。打开 [Swagger UI](http://127.0.0.1:8000/docs)。演示结束后用 Ctrl+C 停止服务并关闭此 PowerShell，环境变量随会话清除。

## 三条 Swagger 演示

1. **查询订单和政策。** `GET /api/orders/O004` 返回本人已签收订单。`GET /api/orders/O014` 和 `GET /api/orders/O404` 返回相同的 404。调用 `GET /api/policies/search`，参数 `q=换货`、`category=数码`、`as_of=2026-11-02`。在候选中找到 `digital-exchange` 的 `v2`，已失效的 `v1` 不会返回。词法候选可能包含超期处理政策，读者需核对条文的适用条件。
2. **取消草稿。** 向 `POST /api/after-sales/drafts` 发送下方草稿字段并记下返回的 `id`。向该草稿的 `/cancel` 发送 `{"expected_version":1}`，响应状态为 `cancelled`。随后向同一草稿的 `/confirm` 发送下方确认字段，预期 409。`GET` 该草稿的 `/application` 返回 404。
3. **确认并重试。** 再创建一张草稿，使用新 `id` 调用 `/confirm`。把 `confirmed` 设为 `false` 时返回 409，设回 `true` 时返回 `pending_review` 和申请 ID。原确认请求再发一次，响应与首次成功结果相同。`GET` 同一草稿的 `/application` 也会返回这份申请，草稿状态为 `submitted`。`pending_review` 表示已登记并等待处理。

草稿请求体

```json
{
  "order_id": "O004",
  "item_id": "O004-I01",
  "request_type": "exchange",
  "reason": "耳机左侧无声",
  "as_of": "2026-11-02"
}
```

确认请求体

```json
{
  "expected_version": 1,
  "confirmed": true,
  "facts": {"issue_type": "malfunction"}
}
```

## 复现 Agent 对话验收

以下测试使用临时业务库和 Checkpoint，经过实际 HTTP 或 LangGraph 链路。模型与向量客户端使用模拟响应。

```powershell
.venv/Scripts/python -m pytest -q -p no:cacheprovider -W error backend/tests/agent/test_after_sales.py
.venv/Scripts/python -m pytest -q -p no:cacheprovider -W error backend/tests/api/test_conversation_api.py -k 'two_turns or prepares_displays or memory_commands'
.venv/Scripts/python -m pytest -q -p no:cacheprovider -W error
```

对话用例覆盖检索与引用、草稿展示、普通“好”不提交、取消后拒绝确认、订单变化后拒绝提交，以及重开 Checkpoint 后确认和重复提交。后端另有并发确认、确认与取消竞争、提交失败回滚、Checkpoint 异常后查询已提交结果的测试。

## 首批需求核对

以下编号对应[原始需求](requirements.md#5-首批验收案例)，与核心 RAG 评测数据中的 Agent 用例编号各自独立。

| 需求 | 本次交付证据 | 范围 |
| --- | --- | --- |
| A01、A02、A04 | 本人订单返回 200，他人和不存在的订单返回相同 404 | 空库 HTTP 已验证 |
| A03 | 多轮对话在缺关键事实时可追问，订单列表 API 按当前用户隔离 | Agent 尚无候选订单列表工具，原案例未完整实现 |
| A05 | 多轮咨询以本轮检索结果校验政策引用，通过准备和确认才登记申请 | 离线集成和固定真实流程已跑通，答复仍有无依据示例 |
| A06、A07 | 时限不满足、缺政策或声明时拒绝登记，回答校验要求本轮证据 | 业务拒绝已验证，自由文本解释仍需真实模型复核 |
| A08 | 草稿可明确取消，取消后确认失败且无申请记录 | HTTP 和对话指令已验证，自由文本“不申请了”不自动取消 |
| A09、A10 | 指定版本确认后可查询申请，相同确认返回同一申请，并发仍只有一份 | 空库 HTTP 和离线并发测试已验证 |

本次空库实测完成初始化、政策导入、Swagger 路由响应及三条 HTTP 演示，服务进程中两个模型 Key 均未配置。全量离线回归 872 项通过。早期模型评估及偏好实测分别见[核心评估](core-acceptance-2026-11-13.md)和[记忆验收](memory-acceptance-2026-11-17.md)。

## 真实模型售后流程复测

2026-11-18 基于 `7cf8f75`，使用全新临时库、仓库模拟订单及 8 个自编政策片段。通过 FastAPI TestClient 调用现有 HTTP 路由，使用真实百炼 `qwen-plus` 和 `text-embedding-v4`，同一会话依次完成下列步骤。

| 步骤 | 观察结果 | 对话 / 向量请求 |
| --- | --- | ---: |
| 建索引 | 8 个片段写入临时库 | 0 / 1 |
| 咨询 O004 / O004-I01 的单侧无声换货 | 模型调用订单与政策工具，引用 `digital-exchange:v2:000`，使用签收 3 天和 15 日时限，草稿与申请均为 0 | 2 / 1 |
| 明确申请换货 | 模型调用准备草稿工具，订单、商品、诉求与用户自述的功能故障匹配，草稿保存后申请仍为 0 | 1 / 0 |
| 确认、重发同一指令、查询回执 | 都返回 200，回执状态为 `pending_review`，重复确认响应一致，数据库只有 1 份申请 | 0 / 0 |

总用量为 3 次对话、2 次向量请求，包含建索引。原有业务数据库未读取或写入。原始轨迹保存在 Git 忽略的 `data/private/eval/after-sales-live-2026-11-18.json`。

人工复核发现咨询答复加入“出厂缺陷或运输导致损坏”的例子，引用条文并未列这些成因。答复保留了质量问题须经核实的条件，草稿也正确记录为用户自述。该固定案例通过流程、引用来源和提交检查，内容忠实度仍有缺口。其他诉求、改写和边界场景仍以各自的离线覆盖为准。

同一咨询加严提示后进行了真实复测，使用 2 次对话、2 次向量请求（含建索引）。仍出现原文未列的“产品缺陷”。随后让数码换货 v2 的单片段咨询结果由代码展示原文，签收天数仅来自本轮匹配订单的工具结果。离线回放该次模型候选，交付答复保留 3 天、原文及确认限制，去掉候选自行补充的故障成因。其他政策仍使用原有生成和校验路径。

过期草稿现在通过「查看售后」明确显示已过期，且不再展示提交指令。过期结果与工具回执一起保存，后续重读历史无需按新时间重新生成旧回答。对话回归覆盖连续查看与拒绝确认，也检查业务库没有申请记录。
