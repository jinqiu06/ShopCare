# ShopCare

面向电商售后场景的 Agent 后端项目。当前已完成订单查询、词法/语义检索、单次回答HTTP、只读工具及LangGraph有界循环；已实现按用户隔离的会话、SQLite持久化短期状态和多轮CLI，并完成模拟模型的跨进程恢复验证。真实模型已跑通一组两轮样例，验证重新检索与咨询能力边界；内容一致性仍有局限，尚未通过完整效果验收。会话HTTP已通过模拟模型的两轮离线验证；长期记忆和售后申请尚未完成。

## 当前功能

- 基于 FastAPI 提供订单列表、订单详情和独立商品项接口。
- 订单查询同时限制订单归属，其他用户订单与不存在订单统一返回 404。
- 列表按订单 ID 稳定排序，支持 limit / offset 分页和参数校验。
- SQLAlchemy 模型包含外键、状态、非负金额和正数数量约束；SQLite 连接显式开启外键检查。
- 提供可重复运行的模拟数据命令：2 名用户、20 笔订单、40 个商品项；已有记录不覆盖。
- Pydantic 配置支持根目录 `.env` 和环境变量覆盖，缺少模型 Key 不影响订单查询。
- 已实现售后意向草稿的创建、按归属查询和版本条件取消服务；尚未接HTTP/Agent，不创建正式申请。
- 754 项离线测试（2026-11-11）覆盖配置、数据库、事务、服务/API、Agent、售后草稿、跨进程恢复、原文摘录校验及预算内回答纠正。

## 环境与安装

当前支持 Python 3.13–3.14，本机验证环境为 Windows / Python 3.13；依赖快照基于该环境。无需前端或 Node.js。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock.txt
.venv/Scripts/python -m pip install --no-deps -e .
Copy-Item .env.example .env
```

如已有 `.env`，不要覆盖本机配置。macOS / Linux 使用 `.venv/bin/python` 替换 `.venv/Scripts/python`。

所有命令从项目根目录执行。`.env` 与 SQLite 相对路径均以当前工作目录为基准。订单查询和词法搜索无需模型 Key；向量构建、语义检索和单次回答需配置对应模型服务，真实请求会产生用量。

## 初始化和运行

```powershell
.venv/Scripts/python -m shopcare.scripts.seed
.venv/Scripts/python -m uvicorn shopcare.main:app --reload --host 127.0.0.1 --port 8000
```

- API 文档：[Swagger UI](http://127.0.0.1:8000/docs)
- 健康检查：[health](http://127.0.0.1:8000/api/health)

初始化默认写入 `data/shopcare.db`，也可指定独立数据库：

```powershell
.venv/Scripts/python -m shopcare.scripts.seed --database-url sqlite:///./data/demo.db
```

命令创建缺失目录和表，数据准备在一个事务内提交，失败回滚。首次新增数为 2 / 20 / 40，再次执行均为 0；不支持并发初始化或已有冲突数据修复。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/orders?limit=3&offset=0` | 分页读取当前用户订单 |
| GET | `/api/orders/{order_id}` | 查询当前用户的指定订单 |
| GET | `/api/orders/{order_id}/items` | 按需查询该订单商品项 |
| POST | `/api/orders/{order_id}/answer` | 对选中商品进行单次政策咨询 |
| POST | `/api/conversations` | 创建会话，固定模拟参照日期并登记空检查点 |
| GET | `/api/conversations/{conversation_id}` | 按当前用户归属读取会话状态和参照日期 |
| POST | `/api/conversations/{conversation_id}/messages` | 发送问题，执行一轮Agent，返回PolicyAnswer |

limit 默认 20，范围 1–100；offset 默认 0，最小 0。参数不合法时返回 422。列表响应仍为数组，不包含总条数。

当前用户依赖固定为模拟用户 U001，**尚未实现登录认证，不适用于真实用户系统**。客户端传入 user_id 或 X-User-ID 不会切换身份；测试通过依赖覆盖验证 U002 的访问隔离。

订单详情与商品项分开读取；可访问的订单无商品项时返回 200 和空列表。模型响应字段白名单不包含用户归属等内部字段。

错误结构：

```json
{
  "error": {
    "code": "ORDER_NOT_FOUND",
    "message": "订单不存在或不可访问"
  }
}
```

未知路由、方法错误和参数校验分别使用 NOT_FOUND、METHOD_NOT_ALLOWED、VALIDATION_ERROR。参数校验错误不回显原始输入；未预期内部异常尚未统一处理。

## 后端结构

```text
backend/
├── src/shopcare/
│   ├── main.py               # 应用组装、生命周期与路由注册
│   ├── api/
│   │   ├── health.py         # 健康路由
│   │   ├── orders.py         # 订单查询与回答 HTTP 参数
│   │   ├── policies.py       # 词法检索 HTTP 路由
│   │   ├── dependencies.py   # 请求级 Session、演示身份
│   │   └── errors.py         # HTTP 错误响应
│   ├── core/config.py        # 环境配置
│   ├── db/
│   │   ├── base.py           # ORM 基类
│   │   ├── session.py        # Engine、外键配置、显式建表
│   │   └── models.py         # 用户、订单、商品项
│   ├── schemas/              # orders.py 响应模型、policy_models.py 政策格式
│   ├── services/
│   │   ├── orders.py         # 订单业务查询
│   │   ├── order_answers.py  # 订单 → 检索 → 回答的固定编排
│   │   ├── answer_generation.py # 消息构造、回答与引用校验
│   │   ├── embeddings.py     # embedding 客户端与余弦计算
│   │   ├── llm.py            # 对话模型客户端
│   │   └── policies/        # 政策领域
│   │       ├── loader.py    # 读取、校验政策
│   │       ├── filter.py    # 内存政策类别/日期过滤
│   │       ├── chunker.py   # 原文切分
│   │       ├── store.py     # 片段入库
│   │       ├── embeddings.py # 政策向量生成、保存与读取
│   │       ├── lexical_retriever.py # 候选查询与词法检索
│   │       └── semantic_retriever.py # 语义检索
│   ├── agent/               # 状态/节点/图、消息/回答校验、agent.py 多轮执行与事务
│   ├── tools/               # orders.py、policy.py、executor.py：工具适配和派发
│   └── scripts/             # 初始化、政策导入、索引构建、评测与演示命令
└── tests/
    ├── api/                  # 健康检查、接口与错误
    ├── agent/                # 节点与完整循环、预算和失败终止
    ├── core/                 # 配置
    ├── db/                   # 数据约束、回滚与初始化
    ├── scripts/              # Agent演示命令的预览、真实工具链和失败验证
    └── services/             # 订单、回答、客户端与命令验证
        └── policies/         # 政策加载、切分、存储、向量与检索验证
```

业务服务接收调用者管理的 Session，不自行提交事务；API 决定 HTTP 响应，schema 决定公开字段。应用启动创建 Engine，按请求创建 Session，关闭时释放资源；启动不自动建表或重置数据。

## 验证

```powershell
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check backend
.venv/Scripts/python -m ruff format backend --check
.venv/Scripts/python -m pip check
```

测试使用临时数据库和模拟数据，不调用模型服务，不修改演示数据库。CI 配置为 Windows / Linux、Python 3.13，以实际运行结果为准。

## 模拟政策与后续开发

`data/policies/` 包含八份自编 JSON 政策，覆盖数码、配件及新旧版本，仅用于项目演示，不代表真实商家承诺或法律要求。已实现政策加载、唯一版本检查、空白字段与日期区间校验，以及按显式日期和精确类别过滤；已实现带字符位置与元数据的固定窗口切分；已实现按政策版本整体替换的片段入库；已实现有效候选查询、相邻双字/英文特征评分、稳定排序和 top_k 词法检索服务；已提供检索 HTTP 接口及小样本基线报告；模型回答尚未实现。

模拟订单采用固定 UTC 时间约定，包含 2026-10-30 的签收记录。演示政策判断拟使用固定参照日期 2026-11-02；当前 SQLite 时间响应不带时区标记。

政策加载/切分、词法/语义检索、固定模型回答及有界工具循环已按阶段完成。后续任务包括真实Agent试跑、多轮会话、人工确认、重启恢复、长期记忆和固定案例评测；尚未实现的能力不计入当前功能。

- [需求与接口约定](docs/requirements.md)
- [数据模型](docs/data-model.md)
- [政策资料规范](docs/policy-data.md)
- [开发规划](docs/开发规划.md)
- [进度记录](docs/progress.md)

本地 `.env`、虚拟环境、数据库、日志和真实用户数据不提交 Git；只提交代码、依赖、文档与公开模拟资料。

## 导入政策片段

```powershell
.venv/Scripts/python -m shopcare.scripts.ingest_policies
```

命令按顺序调用加载、切分与入库服务，在同一事务中提交整个批次。八份默认样例产生八片；重复导入不增加总片段数，重新切分会清理该版本的旧尾部。所有历史版本均保留供查询时过滤。支持 --policy-dir、--database-url、--chunk-size、--overlap；移除源文件不会自动删除库中的旧政策。

## 政策检索 API 与初始基线

`GET /api/policies/search` 接收必填 q（1–500 字符）、category（1–50 字符）、as_of（日期），以及 top_k（默认3，范围1–10）。先筛选类别和生效日期，再按中文相邻双字/英文词块的命中数量评分，正分结果稳定排序后返回前 top_k 条。无证据返回 200 和空列表。

每条结果包含 chunk 和 score；chunk 保留政策 ID、版本、正文、日期和字符位置。score 是字面特征命中数，不是概率或置信度。

在 Swagger UI 中可使用问题“耳机单侧无声换货”、类别“数码”、日期2026-11-02、top_k=1，预期返回 digital-exchange:v2。

```powershell
.venv/Scripts/python -m shopcare.scripts.evaluate_retrieval
```

复现命令使用独立临时数据库，通过 HTTP 路由运行 [十个固定问题](data/eval/retrieval_baseline.json)，不修改演示库。结果见 [检索基线报告](docs/retrieval-baseline.md)：九个有目标政策案例 Hit@1 与 Hit@3 均为8/9，一个预期无结果案例正确返回空列表。口语改写案例未命中，说明词法基线的同义表达局限。本组问题仅用于初始开发诊断，不代表真实场景准确率，也不是独立保留评测集。


## 构建政策向量索引

先使用政策导入命令准备片段，再预览并构建向量：

```powershell
.venv/Scripts/python -m shopcare.scripts.build_policy_index --dry-run
.venv/Scripts/python -m shopcare.scripts.build_policy_index
```

预览不调用模型或修改数据库。正式构建会将数据库中所有政策片段（包括历史版本）的标题和正文发送给配置的 embedding 服务，按客户端每批 10 条处理，产生 API 用量。配置使用 EMBEDDING_MODEL_ID、EMBEDDING_BASE_URL、EMBEDDING_DIMENSIONS；EMBEDDING_API_KEY 留空时复用 LLM_API_KEY。

模型调用全部完成后，复用核心保存服务在一个事务中替换本批向量。重复运行仍会调用模型，但不会增加重复记录；调用或写入失败保留原向量。调用期间检测到片段变化会拒绝保存，需要重跑。重新导入政策会级联删除对应向量，需要重新构建；更换模型或维度也需要重建。支持 --database-url 指定已有本地 SQLite 文件。当前检索 API 仍是词法检索，语义查询尚待接入。


## 语义检索与词法对照

语义搜索服务先复用类别和生效日期过滤，检查候选向量的模型、维度和完整性，再生成问题向量，按余弦相似度和片段 ID 稳定排序。当前 HTTP 搜索接口仍使用词法服务；语义服务用于订单回答，也可通过评测命令直接调用。

```powershell
# 只预览，不调用模型
.venv/Scripts/python -m shopcare.scripts.compare_retrieval
# 实际运行：最多 10 次问题向量调用，复用已保存政策向量，不修改数据库
.venv/Scripts/python -m shopcare.scripts.compare_retrieval --run
```

2026-10-30 十个固定开发案例中，九个正例的 Hit@1/Hit@3：词法均为 8/9，语义均为 9/9；一个无关问题的正确空返回：词法 1/1，语义 0/1。口语换货问题得到改善，但没有相关性阈值的语义检索仍会向无关问题返回候选。本组小样本不是独立保留集，不代表真实业务准确率；本轮没有据此调阈值或添加混合检索。

详见 [对照报告](docs/retrieval-comparison.md) 和 [逐题结果](docs/retrieval-comparison.json)。报告记录模型、维度、案例/索引指纹、候选与分数、单次耗时及调用数；两种分数不能直接比较，单次耗时也不是性能基准。网络错误会终止评测，不作为未命中计入统计。


## 项目结构与调用流程

见 [结构、运行方式与持续更新的函数流程图](docs/architecture-flow.md)。文档区分现有 HTTP 入口、数据准备命令与开发中的单次回答流程；新增函数时同步维护。


## 单次订单咨询演示（开发中）

```powershell
.venv/Scripts/python -m shopcare.scripts.answer_order
.venv/Scripts/python -m shopcare.scripts.answer_order --run --output docs/answer-demo.json
```

默认预览检查固定演示用户 U001 的订单与目标商品，不调用模型；--run 最多调用一次问题 embedding 和一次对话模型，不修改数据库。默认订单 O004、商品 O004-I01，参照日期为模拟日期 2026-11-02；可用 --order-id、--item-id、--query、--as-of 调整。回答 HTTP 接口已接入，见下文；已完成离线接口验证及一次用户Swagger真实调用。

首次真实试跑见 docs/answer-demo.json：格式和引用检查通过，但回答存在对尚未实现的草稿功能作承诺、故障定性过强的问题，内容验收仍在修正中。usage 为对话模型报告值，缺失时记为不可用，不包含 embedding 用量。详见 [流程图](docs/architecture-flow.md)。

单次回答最新进展：已由业务代码计算签收后天数并加入模型输入；模糊故障的单例回归正确澄清且无日期矛盾。原始失败与修订结果见 [回答试跑记录](docs/answer-baseline.md)。最终版本未重新验证所有真实案例，不能据单例声明回答可靠性；当前仍为固定流程咨询，不执行申请。


## 订单回答 HTTP 接口（2026-11-01）

`POST /api/orders/O004/answer` 接收以下 JSON：

```json
{"item_id":"O004-I01","query":"耳机坏了，能换货吗？","as_of":"2026-11-02"}
```

问题长度1–1000、商品ID长度1–100，去除首尾空白并拒绝空白值；as_of 必填日期，2026-11-02 是模拟参照日期；额外字段被拒绝。身份来自服务端固定演示用户U001，商品类别从该用户的订单商品读取。响应为 PolicyAnswer：status、answer、citation_chunk_ids、limitations，不返回内部 AIMessage。

需先准备订单、政策片段及匹配模型/维度的向量索引，并配置 embedding 与对话服务。发送有效请求可能产生一次问题 embedding 和一次对话调用，不写数据库；启动API本身不调用模型。当前客户端无自动重试。

| HTTP状态 | 错误码 | 含义 |
|---|---|---|
| 422 | VALIDATION_ERROR | 请求结构或字段不合法 |
| 404 | ORDER_CONTEXT_NOT_FOUND | 订单或商品不存在或不可访问 |
| 503 | ANSWER_NOT_CONFIGURED | 配置或客户端构造校验失败 |
| 504 | ANSWER_TIMEOUT | 模型调用超时 |
| 502 | ANSWER_UPSTREAM_ERROR | 模型服务调用失败 |
| 500 | ANSWER_FAILED | 回答链路校验或数据库失败 |

配置构造先于订单归属检查，配置失败时优先返回503。参照日期早于签收日目前也按回答阶段校验失败返回500，尚无专门的业务日期错误。模型输出不合规不会转换成正常的 insufficient_evidence 答案。接口仅解释政策，不创建申请。

本次完整离线回归346项通过；HTTP完整链路使用临时数据库和模拟外部模型，覆盖正常输出、非法JSON、伪造引用和缺失索引。用户随后在Swagger完成一次真实调用：HTTP200、needs_clarification、未创建申请，签收3天无日期矛盾；limitations包含政策解释但引用为空，保留可追溯性问题。单例不能证明整体生成可靠性；此前CLI真实试跑的局限仍然成立。


## 只读工具与统一执行入口（2026-11-02）

开发者实现 `tools/orders.py`、`tools/policy.py`、`tools/executor.py`，复用现有订单与语义检索服务。工具可以独立调用，也已供下述LangGraph循环和Agent演示命令使用；尚未接入HTTP工具路由或多轮会话。目录职责和完整调用图见 `docs/architecture-flow.md`。

| 工具 | 模型可填写的参数 | 服务端绑定的上下文 | 成功数据 |
|---|---|---|---|
| lookup_order | order_id | Session、user_id | 公开订单字段 |
| search_order_policies | order_id、item_id、query | Session、embedding_client、user_id、as_of | 政策片段和分数列表 |

参数模型严格拒绝额外字段和错误类型；空白值由工具返回INVALID_ARGUMENT。政策类别从已验证归属的商品读取，模型不能指定user_id、category或as_of。政策证据保留来源和版本；无候选是成功空列表，不等于最终确认没有相关政策或用户没有资格。

在服务端已有Session和客户端的执行上下文中，可按下面方式组装工具。该示例不是单独可运行的启动脚本；Session需要由调用方保持打开。

```python
from shopcare.tools.orders import build_order_tools
from shopcare.tools.policy import build_policy_tools
from shopcare.tools.executor import execute_tool

entries = build_order_tools(session, user_id) + build_policy_tools(
    session, embedding_client, user_id, as_of
)
tools = {entry.name: entry for entry in entries}
result = execute_tool(tools, "lookup_order", {"order_id": "O004"})
```

execute_tool先查工具名称，再单独校验输入，最后invoke。成功返回`{"ok": true, "data": ...}`；失败返回`{"ok": false, "error": {"code": ..., "message": ...}}`。

| 错误码 | 含义 |
|---|---|
| UNKNOWN_TOOL | 工具名未注册 |
| INVALID_ARGUMENT | schema不合法或字段仅为空白 |
| ORDER_NOT_FOUND | 订单不存在或不可访问 |
| ORDER_CONTEXT_NOT_FOUND | 订单/商品不存在或不可访问 |
| TEMPORARILY_UNAVAILABLE | 订单查询数据库失败 |
| RETRIEVAL_UNAVAILABLE | 政策工具的数据库、模型服务或检索校验失败 |

工具不管理外层事务，不自行commit/rollback/close。数据库错误返回后，执行方仍需按事务状态结束或恢复Session，不能盲目继续复用。RETRIEVAL_UNAVAILABLE不承诺故障一定短暂：索引缺失或模型维度不匹配需要修复，不应无限重试。execute_tool不把工具内部所有异常伪装成输入错误，意外执行异常留给后续执行层处理。

本轮384项离线测试通过，未新增真实模型调用；政策工具正常检索可能调用一次问题embedding，不调用对话模型，不写数据库。尚未验证真实模型选工具或Agent循环。


目录整理（2026-11-02）：政策服务统一放在 `services/policies/`，对应测试位于 `backend/tests/services/policies/`。旧的 `shopcare.services.policy_*` 导入已更新为 `shopcare.services.policies.*`，不保留旧路径转发模块。HTTP接口、CLI命令、数据格式和函数名不变；自定义本地代码若使用旧导入，需同步更新。模块输入输出和两条处理流程见 `docs/architecture-flow.md`。


## LangGraph有界工具循环（2026-11-03，离线验收）

`agent/state.py`声明消息与计数；`agent/nodes.py`提供模型/工具节点；`agent/graph.py`组装输入校验、模型、工具及终止路由。现有`POST /api/orders/{order_id}/answer`和answer_order CLI仍使用固定回答流程，没有切换到Agent。

在服务端已有对话客户端与可信身份绑定工具的上下文中，可以独立调用：

```python
from langchain_core.messages import HumanMessage, SystemMessage
from shopcare.agent.graph import build_agent_graph

agent = build_agent_graph(chat_client, tools)
result = agent.invoke({
    "messages": [
        SystemMessage(content="仅查询模拟订单和政策；事实必须来自工具，不承诺创建申请。"),
        HumanMessage(content="订单O004的O004-I01耳机坏了，能换货吗？"),
    ],
    "model_calls": 0,
    "tool_calls": 0,
    "stop_reason": "running",
})
```

示例中的chat_client/tools需由服务端创建；Session在运行期间保持打开，user_id与as_of由工具工厂绑定，不能信任模型参数。bind_tools与compile本身不发模型请求；invoke可能调用真实对话与embedding服务。本日仅运行模拟客户端测试，没有真实付费试跑。

默认最多3次对话模型调用尝试、4次工具派发尝试；单条工具JSON最多12000字符。三个配置都严格要求正整数且拒绝bool，重复工具名称被拒绝。每轮输入要求非空消息、末条HumanMessage、两个计数为整数0、stop_reason为running。默认不带保存器时为独立单次运行；多轮入口通过可选checkpointer加载历史，每轮仅追加新消息并重置调用计数，不直接传回非零计数状态。

模型请求工具后才执行工具，ToolMessage以tool_call_id对应请求；模型可根据结果选择下一工具。未知工具/参数错误作为工具结果供模型修正；数据库/检索基础设施错误、执行或序列化异常、超长输出直接failed结束，不再调用模型。整批预算不足时一个也不派发；对话预算耗尽时也不执行没有后续对话机会的工具。失败尝试计入预算。

终止状态为completed、budget_exhausted或failed。completed只代表模型结束工具循环，不代表事实/引用正确：图仍返回原始消息状态。11月4日新增的run_agent命令在图结束后调用parse_agent_answer，复用PolicyAnswer结构及引用解析器；直接使用图的调用方也应执行这一步。失败/超额时可能保留未配齐结果的工具请求，这种状态不得直接当作可恢复会话继续请求模型。

图设置与模型预算相称的LangGraph步数上限作额外保护；每次工具输出限制是字符数，不是token上限或总历史限制。异常当前只记录通用failed，没有详细诊断事件；工具不自行rollback/close，数据库故障后的Session由外层结束或恢复。

离线验证：

```powershell
.venv/Scripts/python -m pytest backend/tests/agent -q -W error
```

Agent测试67项通过，完整后端451项通过。模拟模型依据真实订单工具结果选择政策工具，真实服务/临时数据库产生政策证据后模型结束；验证直接回答、未知/无效工具参数、模型/工具预算、输入校验和失败不再请求模型。不据此宣称真实模型工具选择或生成内容可靠性已验收。

## 独立Agent演示与回答校验（2026-11-04）

在项目根目录运行：

```powershell
# 只预览：不创建模型客户端、不打开数据库、不写报告
.venv/Scripts/python -m shopcare.scripts.run_agent
# 显式运行：可能产生对话及问题向量用量，保存成功或失败报告
.venv/Scripts/python -m shopcare.scripts.run_agent --run --output data/private/agent-demo.json
```

默认问题为“订单O004的O004-I01耳机坏了，能换货吗？”，参照日期固定为模拟日期2026-11-02。可用--query和--as-of修改；身份由服务端固定演示依赖提供U001，不能通过模型工具参数覆盖。预览只检查输入和本地SQLite文件存在，不校验订单、索引、凭据或服务连通性。预览忽略--output。

正式运行需要已有订单、政策片段、匹配模型/维度的政策向量，以及对话和embedding配置；命令不建表、初始化数据或重建索引。复用两个只读工具，Session启用SQLite query_only，退出关闭Session并释放Engine。不会创建售后申请。现有HTTP和answer_order命令仍使用固定回答流程。

默认最多3次对话尝试、4次工具派发尝试，问题向量调用至多4次（实际可能更少或为0）；客户端无自动重试。用--max-model-calls、--max-tool-calls、--max-output-chars调整正整数预算，单条工具JSON默认最多12000字符。预算是次数/单条字符限制，不是总token、总成本或整次运行时限。

调用链为build_agent_messages → build_agent_graph.invoke → parse_agent_answer → collect_policy_evidence/parse_policy_answer。仅允许正常完成、最终模型消息无工具请求、JSON结构正确的回答；引用必须来自本次匹配的成功政策工具结果，不能重复，answered必须有引用。其他状态下出现政策解释却未引用，以及政策解释/业务资格/事实是否正确，仍需内容审查。

图返回后报告包含stop_reason、模型/工具尝试次数、工具请求名称/编号、耗时、PolicyAnswer和各已保留模型响应的原始usage。缺失usage为null，不推算失败尝试或embedding用量，不输出私有思考、原始订单或完整消息历史。失败/预算耗尽为AGENT_RUN_FAILED；completed但回答校验失败为ANSWER_INVALID。两者answer均为null、answer_validated为false，进程返回1；校验成功返回0。图开始前的输入/配置/数据库等错误仅输出通用错误，不生成运行报告。

本轮已完成模拟模型、临时数据库的离线链路验证和本地默认预览，未调用真实模型。answer_validated表示结构与引用来源检查通过，不表示全部语义已验证。新增文件与调用图见docs/architecture-flow.md。

## 持久化多轮会话（2026-11-05，离线验收）

```powershell
# 只预览，不创建会话、检查点、模型客户端或报告
.venv/Scripts/python -m shopcare.scripts.chat_agent
# 实际运行首轮，创建会话并返回conversation_id；会产生模型用量
.venv/Scripts/python -m shopcare.scripts.chat_agent --run --output data/private/chat-first.json
# 将下面的编号替换为首轮返回的conversation_id，继续同一会话
.venv/Scripts/python -m shopcare.scripts.chat_agent --run --conversation-id "替换为首轮返回的编号" --query "左边没声音" --output data/private/chat-second.json
```

默认新会话参照日期2026-11-02，创建时可用--as-of设置；继续会话时固定使用数据库里的日期，禁止覆盖。演示身份固定为服务端U001。检查点默认保存到data/private/agent-checkpoints.db，可用--checkpoint-db指定独立SQLite文件，必须与业务数据库不同；后续继续时使用同一个文件。已有会话找不到对应检查点时，命令拒绝继续，不会悄悄新建空历史。

命令需要已有业务数据库和模拟用户/订单；正式运行会补建缺失表，以保存conversations记录，不生成或重置订单/政策。咨询检索仍需准备政策片段与匹配的向量。只保存会话及图检查点，不执行申请/退款。预览仅检查参数和数据库文件存在，不校验会话、索引或模型连通性。

run_conversation_turn先检查输入，在短事务中取得执行权并提交running；新的查询Session执行图并校验回答；再用独立事务提交ready或failed。归属检查先于检查点访问，running/failed会话不能启动新轮次。成功询问补充信息也回到ready，允许下一轮用户消息；每轮默认3次模型/4次工具尝试，历史保留、计数重置。历史只用于理解上下文，本轮政策引用必须来自本轮重新检索的成功结果。

输出包含conversation_id、会话状态、固定日期、耗时、结构化回答和校验结果。图执行或收尾失败时answer为null、返回码1；状态读取失败记为null，不假装已写入failed。参数/配置/归属等运行前错误仅输出通用错误。报告和检查点放在已忽略的data/private目录，避免提交聊天内容。

已用两个独立Python进程运行真实CLI、业务服务、工具和SQLite保存器，验证“首轮澄清→进程退出→第二轮加载上下文并检索政策”。外部chat/embedding使用Mock，尚未验证真实模型多轮效果。检查点和业务会话表不构成跨数据库原子事务；执行中崩溃或状态提交失败可能留下running，当前需人工检查，不自动解锁/重试。暂未做消息裁剪、摘要、长期偏好记忆、会话HTTP或售后申请。

## Agent原文摘录校验（2026-11-09）

Agent内部最终JSON增加必填`policy_quotes`，每项为`chunk_id`与对应本轮政策`text`的连续原文`quote`；摘录编号集合必须与引用编号集合一致。校验及既有引用/咨询边界检查通过后，CLI的`answer`仍是原四字段`PolicyAnswer`，摘录保留在内部消息/检查点中。固定HTTP回答入口没有接入该项检查。

`chat_agent`继续旧会话时，如果已完成回答缺少`policy_quotes`，会提示新建会话，并保留原状态和历史；不自动补写或伪造摘录。模型提示和预算内纠正流程已同步，但新版真实模型试跑尚未执行。原文摘录匹配只证明出处与文本一致，不能保证回答中的全部推论正确。

## 会话消息HTTP（2026-11-10，模拟模型离线验收）

先POST /api/conversations，传入 {"as_of":"2026-11-02"}，保存返回的id；再POST /api/conversations/{id}/messages，传入 {"query":"订单O004的O004-I01耳机坏了"}。后续问题继续使用同一个id；可信身份固定为演示用户U001，参照日期取创建时的会话记录，客户端不能在消息请求中覆盖。

消息响应为PolicyAnswer的status、answer、citation_chunk_ids、limitations四个字段，不公开内部thread_id或模型原始消息。消息服务检查归属、ready与已有历史，管理请求级保存器/HTTP连接，复用run_conversation_turn原子领取和Graph执行。GET /api/conversations/{id}可查询ready/running/failed。

不存在/越权404，非ready或缺历史409，输入422，配置/数据库/检查点打开故障503，模型超时504，上游SDK故障502。执行异常后通常写failed；若进程中断或收尾写库失败仍可能遗留running。未实现幂等重试、自动恢复或售后写操作，请先查询状态，不要把重复发送视为安全重试。

验收仅使用临时SQLite及Mock模型客户端，实际运行HTTP、服务、Graph、工具检索和检查点，证明首轮澄清→第二轮加载历史并检索本轮政策证据、每轮结束ready和连接关闭。此前章节中的“会话HTTP未完成”描述为当时状态，以本节为准；未新增真实模型效果验收。
