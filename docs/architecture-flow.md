# ShopCare 结构与运行流程

核对日期：2026-11-03。图对应当前代码；新增函数时同步维护本文件的对应流程和函数清单。

## 当前状态

- 已运行验证：订单 API、词法检索 API、政策导入、向量构建、语义检索和十题真实对照。
- 单次回答CLI与HTTP入口均已接入并做离线验证。此前CLI真实试跑发现能力承诺和日期问题，最终日期修复仅做一个真实样例回归；本次未新增真实模型HTTP验证。
- HTTP 搜索仍调用词法检索，已有 answer_order CLI 与 POST /api/orders/{order_id}/answer 回答路由。启动 FastAPI 不会自动运行回答函数。
- 订单/政策工具及LangGraph有界工具循环已完成独立离线验收；真实模型Agent试跑、会话记忆和售后申请尚未完成。

## 目录职责

路径均相对于 D:/VsCode/ShopCare。

| 目录/文件 | 职责 |
|---|---|
| backend/src/shopcare/main.py | 组装 FastAPI、注册路由、管理数据库 Engine 生命周期 |
| api/ | 接收 HTTP 请求、校验参数、创建请求 Session、统一错误响应 |
| core/config.py | Settings 从项目根目录 .env 读取配置 |
| db/ | Base、连接/建表、SQLAlchemy ORM 表 |
| schemas/ | Pydantic 输入输出结构与校验；不是数据库表 |
| services/ | 核心函数：订单归属、政策加载/切分/检索、回答与引用校验 |
| tools/ | 模型工具适配：参数说明、绑定可信身份、调用services、结构化结果；已供独立Agent图复用 |
| agent/ | 单次运行状态、模型/工具节点、条件路由和预算终止；未接HTTP或会话持久化 |
| scripts/ | main() 命令入口，组织配置、资源、事务和输出，复用 services |
| backend/tests/ | 离线测试，使用模拟模型和临时数据库 |
| data/policies/ | 八份自编模拟政策 JSON |
| data/eval/ | 固定检索问题与预期政策 |
| data/shopcare.db | 本地 SQLite：订单、政策片段和向量；不提交 Git |
| docs/ | 规划、进度、流程与评测报告 |
| .env / .env.example | 实际本地配置 / 可提交配置模板；仅 .env 自动加载 |

## 全局关系

### 同名模块的分工

| 层与模块 | 面向谁 | 负责什么 | 核心实现负责人 |
|---|---|---|---|
| `api/orders.py` | HTTP 调用方 | 路由、请求依赖、HTTP 状态与响应 | 开发者 |
| `services/orders.py` | API、CLI 和工具 | 按可信用户身份查订单、列表和商品项 | 开发者 |
| `schemas/orders.py` | 各层数据传递 | 公开数据字段、校验与序列化 | 开发者 |
| `db/models.py` | 业务服务 | 数据库表、关系及约束 | 开发者 |
| `tools/orders.py` | 模型工具执行方 | 工具说明和参数、绑定服务端身份、调用订单服务、返回工具结果 | 开发者 |
| `scripts/` | 命令行使用者 | 组织配置、资源、预览和报告，复用核心函数 | 助手维护常规辅助命令；核心算法仍由开发者实现 |
| `backend/tests/` | 开发验证 | 模拟外部服务、临时数据库、参数/隔离/失败回归 | 助手补充，开发者可共同学习和审查 |

`api/orders.py` 与 `tools/orders.py` 是两种入口；共同复用 `services/orders.py`。services 不导入 api 或 tools，不依赖 HTTPException。tools 不调用 HTTP 路由。工具错误码与 HTTP 状态码分别在对应入口转换。

政策服务现已归入 `services/policies/`，顶层保留订单查询、固定回答编排、回答生成和通用模型模块。所有仓库内导入及测试已同步迁移，旧模块路径不再提供。函数名及业务行为不变。

### 模块输入、输出与修改位置

以下路径相对于 `backend/src/shopcare/services/`。

| 模块 | 主要输入 | 主要输出 / 副作用 | 修改场景 |
|---|---|---|---|
| `orders.py` | Session、可信用户、订单号/分页 | ORM订单与商品；只读数据库 | 订单查询和归属规则 |
| `order_answers.py` | 可信用户、订单/商品、问题、日期、客户端 | 可信订单上下文或PolicyAnswer与AIMessage | 固定回答流程如何串联 |
| `answer_generation.py` | 问题、订单事实、证据、日期 | 消息、结构化答案、日期差；可能调用对话模型 | 提示词、引用和回答解析 |
| `embeddings.py` | Settings或两个向量 | embedding客户端或余弦相似度 | 模型配置和基础向量计算 |
| `llm.py` | Settings | 对话客户端；构造时不发请求 | 对话模型超时及客户端配置 |
| `policies/loader.py` | 文件/目录 | 校验后的PolicyDocument | 政策文件格式与读取 |
| `policies/filter.py` | 内存政策对象、类别、日期 | 有效政策对象 | 独立内存过滤；数据库检索不经此模块 |
| `policies/chunker.py` | PolicyDocument、窗口参数 | 带原文位置的PolicyChunk列表 | 切分窗口与重叠 |
| `policies/store.py` | Session、政策与片段 | 写入片段数量；flush、不commit | 按政策版本替换片段 |
| `policies/embeddings.py` | 片段、客户端或Session、向量/模型/维度 | 政策向量生成、保存及读取；可能外部调用或写库 | embedding输入文本和政策向量校验 |
| `policies/lexical_retriever.py` | Session、问题、类别、日期、top_k | 有效候选或词法命中 | Bigram、词法评分、数据库候选过滤 |
| `policies/semantic_retriever.py` | Session、embedding客户端、问题、类别、日期 | 语义候选、分数；可能调用问题embedding | 向量检索与排序 |

`services/embeddings.py` 是客户端及通用计算，`services/policies/embeddings.py` 是政策片段向量业务，两者不合并。`policies/filter.py` 处理内存文档，`lexical_retriever.list_effective_chunks` 在SQL中过滤片段，两种数据对象与调用场景不同。

### 两条流程

```mermaid
flowchart TD
    subgraph PREP["知识库准备：在提问前运行"]
        LOAD["policies.loader"] --> CHUNK["policies.chunker"]
        CHUNK --> STORE["policies.store：保存片段"]
        STORE --> INDEX["build_policy_index命令"]
        INDEX --> EMB["policies.embeddings：生成与保存向量"]
    end
    subgraph QUERY["提问时：现有固定回答链路"]
        Q["order_answers：订单归属和商品事实"] --> SEARCH["policies.semantic_retriever：类别/日期 → 向量读取 → 问题embedding → 排序"]
        SEARCH --> GEN["answer_generation：证据和订单事实 → 模型 → 引用校验"]
    end
    EMB -.已保存的政策向量.-> SEARCH
    LEX["词法HTTP搜索/对照命令"] --> BASE["policies.lexical_retriever：数据库候选过滤与词法评分"]
```

词法和语义检索仍可独立对照，当前固定回答使用语义路径；图中不表示两者已做混合排序。政策工具复用订单上下文与语义检索，不自行生成回答。

下方全局图展示现有HTTP/CLI入口；独立工具与Agent调用图见本页11月2日/3日记录。Agent图已实现并独立离线验证，HTTP/CLI仍使用固定回答链路。

```mermaid
flowchart TD
    CFG["Settings / .env"] --> CLI["scripts 命令入口"]
    CFG --> APP["main.py / FastAPI"]
    CLI --> CORE["services 核心函数"]
    APP --> API["api 路由与依赖"]
    API --> CORE
    CORE --> DB["db ORM + Session → SQLite"]
    CORE --> SCHEMA["schemas 数据转换与校验"]
    CORE --> EMB["embedding 服务：文本 → 向量"]
    CORE --> CHAT["对话模型：材料 → 回答"]
```

箭头表示依赖/调用关系，不表示每个函数都会访问数据库或调用模型。schemas 不负责数据库读写，scripts 不另写一套业务算法。

## 数据准备：导入政策与建立向量

```mermaid
flowchart TD
    S["seed.main"] --> SD["seed_demo_data"] --> ODB["users / orders / order_items"]
    I["ingest_policies.main"] --> L["load_policies → load_policy"]
    L --> V["PolicyDocument：字段/日期/版本校验"]
    V --> SPLIT["split_policy → PolicyChunk"]
    SPLIT --> STORE["replace_policy_chunks"] --> PDB["policy_chunks"]
    B["build_policy_index.main"] --> PRE["读取片段；dry-run 到此结束"]
    PDB --> PRE
    PRE --> EC["build_embedding_client"]
    EC --> EV["embed_policy_chunks → embed_documents"]
    EV --> CHK["检查源片段未变化"]
    CHK --> SAVE["save_policy_embeddings"] --> EDB["policy_embeddings"]
    STORE -. "删除旧片段时级联删除对应向量，需重建" .-> EDB
```

导入命令在一个事务中提交整批政策。向量构建先完成模型请求，再在一个事务中保存。核心保存函数只 flush，不 commit；提交和回滚由调用方管理。向量包含 chunk_id、model_id、dimensions、vector，每个片段当前只保存一份向量。

## FastAPI 启动和现有请求

```mermaid
flowchart TD
    U["uvicorn shopcare.main:app"] --> SET["Settings"] --> LIFE["lifespan → build_engine"]
    LIFE --> STATE["app.state.engine"]
    REQ["HTTP 请求"] --> ROUTE["api 路由：参数校验"]
    STATE --> SES["get_session：请求级 Session"] --> ROUTE
    ROUTE --> HEALTH["get_status：健康检查"]
    ROUTE --> ORD["read_orders / read_order / read_order_items"]
    ID["get_current_user_id：固定 U001，演示身份"] --> ORD
    ORD --> OS["list_orders_for_user / get_order_for_user / get_order_items_for_user"]
    ROUTE --> POL["search_policies"] --> LEX["search_policy_chunks：词法"]
    OS --> OUT["响应模型 → HTTP JSON"]
    LEX --> OUT
    ROUTE -. "HTTP/参数错误" .-> ERR["http_error_handler / validation_error_handler"]
    LIFE -. "服务关闭" .-> END["engine.dispose"]
```

启动不会自动建表、导入数据、构建向量或调用模型。固定 U001 是演示配置，不是真实登录认证。

## 订单咨询主流程：正在接通的服务层入口

```mermaid
flowchart TD
    START["answer_order_question：用户/订单/商品/问题/日期"] --> Q{"问题非空？"}
    Q -->|否| FAIL["抛出异常；停止"]
    Q -->|是| CTX["get_order_answer_context"]
    CTX --> OWN["get_order_for_user：订单归属"]
    OWN --> ACCESS{"订单可访问？"}
    ACCESS -->|否| FAIL
    ACCESS -->|是| ITEMS["get_order_items_for_user：订单商品"]
    ITEMS --> MATCH{"item_id 在订单中？"}
    MATCH -->|否| FAIL
    MATCH -->|是| DTO["OrderResponse + 选中的 OrderItemResponse"]
    DTO --> RET["search_semantic_policy_chunks：使用 item.category"]
    RET --> GEN["generate_policy_answer"]
    GEN --> MSG["build_answer_messages：规则 + 订单/商品/证据 JSON"]
    MSG --> DAYS["get_days_since_delivery：在消息中加入明确天数或 null"]
    DAYS --> CALL["chat_client.invoke：一次对话模型调用"]
    CALL --> TYPE["检查 AIMessage + 文本内容"]
    TYPE --> PARSE["parse_policy_answer"]
    PARSE --> FORMAT["PolicyAnswer.model_validate_json"]
    FORMAT --> CITE["非空白回答；引用属于本次证据；不重复；answered 必须有引用"]
    CITE --> RESULT["返回 PolicyAnswer + AIMessage，保留 usage_metadata"]
    RET -. "无有效候选：空证据" .-> GEN
    CALL -. "超时/服务错误" .-> FAIL
    CITE -. "校验失败" .-> FAIL
```

这里是固定代码顺序，尚无 Agent 自主选工具。get_order_answer_context 只准备可信事实；generate_policy_answer 只使用已准备的材料；answer_order_question 把它们与检索串起来。只有 invoke 调用对话模型，创建客户端不发送请求。当前无证据仍会进入生成函数，提示词要求说明不足，解析器拒绝无引用的 answered；不能声称代码已自动验证政策与每句结论的语义一致性。

## 两种检索的内部流程

```mermaid
flowchart TD
    L["search_policy_chunks"] --> TOK["tokenize_query：去重词法特征"]
    TOK --> LF["list_effective_chunks：类别 + 生效日期"]
    LF --> SCORE["score_chunk：标题/正文命中特征计数"]
    SCORE --> POS["仅保留正分；稳定排序；top_k"]
    S["search_semantic_policy_chunks"] --> SF["list_effective_chunks：类别 + 生效日期"]
    SF --> EMPTY{"有候选？"}
    EMPTY -->|否| NONE["返回空；不调用 embedding"]
    EMPTY -->|是| VECS["load_policy_embeddings：模型/维度/完整性"]
    VECS --> EQ["embedding_client.embed_query：问题向量"]
    EQ --> VALID["检查维度、有限数值和非零"]
    VALID --> COS["cosine_similarity：逐片段比较"]
    COS --> TOP["分数降序 / chunk_id 升序 → top_k"]
```

类别和有效期使用 [effective_from, effective_until)；失效日期可为空。语义检索尚无相关性阈值，因此可能向无关问题返回候选。独立 policies/filter.py 的 is_policy_effective / filter_applicable_policies 是早期内存过滤函数，当前数据库检索使用 list_effective_chunks，不重复经过它们。

## 如何运行

在 PowerShell 中切换到项目根目录；若当前显示 >>>，先用 exit() 退出 Python 交互窗口。

```powershell
Set-Location D:\VsCode\ShopCare
# 首次准备或需要补齐模拟数据时运行；已有数据通常不用重复初始化
.venv\Scripts\python.exe -m shopcare.scripts.seed
# 重新导入会清理对应旧向量，不要日常启动时反复执行
.venv\Scripts\python.exe -m shopcare.scripts.ingest_policies
# 免费本地预览；只在政策/模型/维度变化或索引缺失时正式重建
.venv\Scripts\python.exe -m shopcare.scripts.build_policy_index --dry-run
.venv\Scripts\python.exe -m shopcare.scripts.build_policy_index
# 日常启动 API
.venv\Scripts\python.exe -m uvicorn shopcare.main:app --reload --host 127.0.0.1 --port 8000
```

正式 build_policy_index 调用真实 embedding，产生用量。已有索引不需要随 API 启动重建。浏览器访问 http://127.0.0.1:8000/docs 查看实际路由；http://127.0.0.1:8000/api/health 检查服务。政策搜索仍是词法；回答服务可通过独立 answer_order CLI 或 POST /api/orders/{order_id}/answer 调用；启动 API 不自动调用回答服务。

评测另开终端，无需先启动 uvicorn：

```powershell
# 独立临时库，通过 TestClient 验证词法 HTTP 路由；不调用模型
.venv\Scripts\python.exe -m shopcare.scripts.evaluate_retrieval
# 默认预览；--run 使用真实 embedding 问题向量，读取本地索引
.venv\Scripts\python.exe -m shopcare.scripts.compare_retrieval
.venv\Scripts\python.exe -m shopcare.scripts.compare_retrieval --run
```

## 函数定位清单

包含入口、服务、校验方法和数据库连接辅助函数。新增函数应更新对应图或说明，以及此清单；不在主流程执行的函数标明为辅助或独立用途。各脚本的 main 是命令包装，并非另一套业务实现。

| Python 模块（backend/src/shopcare 下） | 当前函数/方法 |
|---|---|
| agent/graph.py | `build_agent_graph`, `validate_start`（内部）, `route_after_model`, `route_after_tools` |
| agent/nodes.py | `build_model_node`, `call_model`（内部）, `build_tool_node`, `call_tools`（内部） |
| agent/state.py | `AgentState`类型声明，无函数 |
| agent/__init__.py | 包声明，无函数、无核心调用 |
| api/dependencies.py | `get_session`, `get_current_user_id` |
| api/errors.py | `http_error_handler`, `validation_error_handler` |
| api/health.py | `get_status` |
| api/orders.py | `read_orders`, `read_order`, `read_order_items`, `answer_order` |
| schemas/answers.py | `OrderAnswerRequest.reject_blank` |
| api/policies.py | `search_policies` |
| db/session.py | `build_engine`, `init_db`, `enable_foreign_keys` |
| main.py | `lifespan` |
| schemas/policy_models.py | `reject_blank`, `validate_date_range` |
| scripts/build_policy_index.py | `main` |
| scripts/compare_retrieval.py | `main` |
| scripts/evaluate_retrieval.py | `main` |
| scripts/ingest_policies.py | `main` |
| scripts/seed.py | `seed_demo_data`, `main` |
| services/answer_generation.py | `build_answer_messages`, `parse_policy_answer`, `generate_policy_answer`, `get_days_since_delivery` |
| services/embeddings.py | `cosine_similarity`, `build_embedding_client` |
| services/llm.py | `build_chat_client` |
| services/order_answers.py | `get_order_answer_context`, `answer_order_question` |
| services/orders.py | `get_order_for_user`, `list_orders_for_user`, `get_order_items_for_user` |
| services/policies/chunker.py | `split_policy` |
| services/policies/embeddings.py | `embed_policy_chunks`, `save_policy_embeddings`, `load_policy_embeddings` |
| services/policies/filter.py | `is_policy_effective`, `filter_applicable_policies` |
| services/policies/loader.py | `load_policy`, `load_policies` |
| services/policies/lexical_retriever.py | `list_effective_chunks`, `tokenize_query`, `score_chunk`, `search_policy_chunks` |
| services/policies/store.py | `replace_policy_chunks` |
| services/policies/semantic_retriever.py | `search_semantic_policy_chunks` |


## 单次回答演示入口（2026-10-31 新增）

```mermaid
flowchart TD
    CLI["answer_order.main"] --> CFG["Settings + 固定演示身份 U001"]
    CFG --> PRE{"是否 --run？"}
    PRE -->|否| CHECK["get_order_answer_context：归属与商品检查"]
    CHECK --> PRINT["打印预览，不调用模型"]
    PRE -->|是| CLIENTS["build_embedding_client + build_chat_client"]
    CLIENTS --> CORE["answer_order_question：既有订单咨询主流程"]
    CORE --> OUT["回答 + 引用 + 总耗时 + 对话 usage"]
    OUT --> FILE["stdout；可选 --output 保存 JSON"]
```

main 不实现新的问答算法，不写数据库；调用异常向 CLI 转换为失败退出，未校验回答不会保存为成功结果。as_of 默认固定 2026-11-02，是模拟场景参照日期，不是系统今天。--run 可能调用一次 embedding 和一次 qwen-plus；两者用量分开，不以对话 usage 代替总用量。构造客户端不发送请求。

新增源码函数：scripts/answer_order.py 的 main。验证说明：test_order_service.py 新增 answer_clients 测试 fixture、完整链路、选中商品类别、越权/空输入不调用模型以及 embedding 失败阻止对话测试；这些测试不进入业务运行图。

```powershell
.venv\Scripts\python.exe -m shopcare.scripts.answer_order
.venv\Scripts\python.exe -m shopcare.scripts.answer_order --run --output docs/answer-demo.json
```

验证更新：订单与回答相关 54 项离线测试通过。answer_order CLI 已实际预览并试跑；docs/answer-demo.json 为首次真实试跑原始记录，虽然格式和引用校验通过，回答的草稿动作承诺与质量问题定性尚未通过内容验收。

复测状态：提示词边界修改后，明确故障/模糊故障仍有动作承诺和状态/日期错误；天气问题返回证据不足。详见 answer-baseline.md。未新增运行函数；调用图不变，内容验收尚未完成。

第二次复测：澄清状态改善，但正文/limitations 的日期和条件仍需修正。计划在消息构造前增加日期差计算，尚未实现，当前调用图不将其画成已存在节点。最终判定见 answer-baseline.md。


## 签收日期差计算（已实现并接入消息）

```mermaid
flowchart TD
    D["get_days_since_delivery：OrderResponse + as_of"] --> KNOWN{"有 delivered_at？"}
    KNOWN -->|否| UNKNOWN["返回 None：未知，不是零天"]
    KNOWN -->|是| DATE["delivered_at.date：当前项目 UTC 日期约定"]
    DATE --> BEFORE{"as_of 早于签收日？"}
    BEFORE -->|是| ERROR["ValueError"]
    BEFORE -->|否| DAYS["返回日历日期差 days"]
```

此函数不判断换货资格，也不把15日解释为具体截止时刻；已由 build_answer_messages 调用，作为 days_since_delivery 字段加入模型输入。对应现有 test_answer_generation.py 验证同日、跨月、闰年、跨年、未知签收和非法参照日期。未调用模型。

当前验收更新：日期事实已接入消息，最新“耳机坏了”一次真实复测正确澄清并引用3天事实，无日期矛盾；未复跑其余旧样例，不能宣称整个当前版本真实案例全部通过。原始失败与修订结果均保留。

## 2026-11-01｜回答请求模型（尚未接入 HTTP）

开发者新增 schemas/answers.py 的 OrderAnswerRequest 与 reject_blank 校验函数：对 item_id、query 去除首尾空白并拒绝空白值，字段声明限制长度，as_of 解析为日期，拒绝额外字段。仅进行请求数据校验，不调用核心服务、数据库或模型；尚未接入路由，不计为可用 HTTP 入口。

源码函数清单补充：schemas/answers.py — OrderAnswerRequest.reject_blank。已本地验证正常输入、空白问题、空白商品、非法日期、超长问题和额外身份字段；未调用外部模型。

## 2026-11-01｜回答 HTTP 成功路径（异常转换待实现）

```mermaid
flowchart TD
    HTTP["POST /api/orders/{order_id}/answer"] --> INPUT["OrderAnswerRequest：字段和空白校验"]
    INPUT --> DEPS["get_session + get_current_user_id：服务端演示身份"]
    DEPS --> CLIENTS["Settings + build_embedding_client + build_chat_client"]
    CLIENTS --> ANSWER["answer_order_question：归属检查 → 语义检索 → 单次回答"]
    ANSWER --> OUTPUT["仅返回 PolicyAnswer，不暴露 AIMessage"]
```

开发者新增源码函数 api/orders.py 的 answer_order，接在 HTTP 入口与现有 answer_order_question 之间，已由 orders_router 接入应用。当前仅成功路径完成；LookupError、配置/索引/输出校验错误及模型服务错误尚未转换为统一 HTTP 错误，暂未完成接口验收。身份仍为固定演示身份，不是真实认证。

助手复用 test_order_api.py 的 client fixture，新增 test_answer_api_forwards_trusted_identity_and_returns_only_answer 与 test_answer_api_rejects_invalid_body_before_clients：验证服务端身份传递、参数去空白、仅返回结构化回答、五种非法输入在构造客户端前拒绝。模拟客户端及核心返回，不调用真实模型；不据此宣称底层越权路径已完成 HTTP 验证。订单 API 文件共37项测试通过，-W error 无警告。初次运行遭遇旧测试缓存/临时目录访问限制，改用独立临时目录并关闭缓存后通过。

### 回答接口 404 验证更新

开发者已在 answer_order 将 LookupError 转为统一 404（ORDER_CONTEXT_NOT_FOUND）；主调用图不变。新增测试函数 test_answer_api_missing_unowned_and_wrong_item_return_safe_404：保留真实 answer_order_question 与归属/商品检查，仅替换外部客户端，覆盖订单不存在、他人订单、商品不属于订单。三例均返回相同错误，且 embedding.embed_query 与 chat.invoke 未调用。订单 API 共40项测试通过，-W error 无警告，相关代码及格式检查通过。客户端构造仍在 try 外，配置、索引和模型异常转换待实现；尚未完成整体接口验收。

### 回答接口上游异常验证更新

开发者在 answer_order 按顺序捕获 APITimeoutError/TimeoutError 和 OpenAIError，分别返回504 ANSWER_TIMEOUT与502 ANSWER_UPSTREAM_ERROR，采用固定对外提示。新增测试函数 test_answer_api_converts_provider_errors_without_leaking_details，模拟三种异常，验证状态、错误结构、不泄漏内部细节、不重试；不调用真实模型。订单 API 共43项测试通过，-W error 无警告。调用图不变，配置构造仍在 try 外，索引/模型输出等 ValueError 尚未处理，整体接口验收仍待完成。

### 回答接口配置错误验证更新

开发者将 Settings、embedding/chat 客户端构造置于独立 try，捕获 ValueError 返回503 ANSWER_NOT_CONFIGURED，不与回答阶段校验错误混淆。新增测试函数 test_answer_api_configuration_failure_stops_before_pipeline，模拟三个配置阶段失败，验证固定错误响应、不泄漏细节、停止后续构造和回答流程。订单 API 共46项测试通过，-W error 无警告，相关代码与格式检查通过，无真实模型调用。主调用顺序不变，回答阶段 ValueError 和数据库异常仍待处理，整体接口尚未验收完成。


## 2026-11-01｜回答 HTTP 离线验收完成

调用路径：HTTP → OrderAnswerRequest → 服务端身份/Session → 配置和客户端构造 → answer_order_question → PolicyAnswer。配置阶段ValueError转503；回答阶段LookupError转404、超时转504、OpenAIError转502、ValueError/SQLAlchemyError转500，均采用固定公开错误。配置构造优先于归属检查；日期早于签收日仍是通用500，后续可细化。历史小步记录中的待实现异常分支现已完成。

新增测试函数 test_answer_api_database_failure_is_safe_error 与 test_answer_api_real_pipeline_with_simulated_models：验证数据库异常，以及保留真实HTTP、订单事实、检索、消息构造和引用校验的正常回答/非法JSON/伪造引用/缺失索引。仅替换外部模型客户端，使用临时数据库；缺失索引不调用模型。运行函数无新增，主调用图不变。

最终离线回归346项通过，-W error 无警告；全后端Ruff与格式检查结果见当日进度。未新增真实付费调用，不把离线通过等同于语义正确或真实模型稳定；仍无Agent、记忆或售后写操作。

## 2026-11-02｜订单工具初稿审查（未验收）

新增源码函数 tools/orders.py 的 build_order_tools 与内部工具 lookup_order。工厂绑定服务端 Session/user_id，工具schema目前仅暴露order_id。预期连接：独立tool.invoke → lookup_order → services.orders.get_order_for_user → OrderResponse；尚未接HTTP或Agent，不改变已有固定回答链路。

初稿实测：空白订单号抛ValueError；正常订单号因遗漏session参数而抛TypeError。成功结果data仍为Ellipsis占位，尚未实现序列化。应由开发者补齐INVALID_ARGUMENT结构化错误、传递session和成功数据转换，之后再做隔离/错误验证。尚未运行通过工具业务测试。源码函数清单补充：tools/orders.py — build_order_tools、lookup_order（内部工具，待验收）。

### 订单工具基本行为验证通过（数据库异常待实现）

开发者已补齐空白订单号的INVALID_ARGUMENT结果、get_order_for_user(session, user_id, order_id.strip())调用，以及OrderResponse JSON序列化。助手仅整理格式并在现有test_order_service.py复用session fixture，新增test_order_tool_invocation_and_safe_output、test_order_tool_schema_and_separate_user_bindings：六项实例覆盖正常查询、去空白、两名用户、他人/不存在订单同错、公开字段与JSON序列化、仅暴露order_id及额外身份参数不切换用户。当前额外字段被框架忽略，不代表已严格拒绝额外参数。

该测试文件41项通过，-W error 无警告；使用临时库，无外部模型调用。调用关系已成立：独立tool.invoke → lookup_order → get_order_for_user → OrderResponse；仍未接Agent/HTTP。数据库异常以及缺字段/错误类型的框架校验转换尚待处理，不宣称整体工具已验收。运行函数无新增，主应用调用图不变。

### 订单工具数据库故障验证通过

开发者已在lookup_order将SQLAlchemyError转换为TEMPORARILY_UNAVAILABLE，保留固定提示，不暴露内部SQL，不自行commit/rollback/close外层Session。现有test_order_tool_database_failure_is_safe_and_keeps_transaction_ownership验证上述边界；订单服务测试文件42项通过，-W error无警告，相关Ruff和格式检查通过。缺失order_id/错误类型实测由LangChain输入校验抛ValidationError，尚未进入工具函数；后续执行层需要统一转换此类框架校验错误。当前额外参数仍被忽略，计划用显式args_schema严格拒绝。无新增运行函数，主调用关系不变。

### 订单工具显式输入模型验证通过

开发者新增schemas/orders.py的OrderLookupInput类（无新增函数），并通过@tool(args_schema=OrderLookupInput)接入lookup_order，严格字符串、长度1–100、拒绝额外字段。助手调整原先“额外身份被忽略”的测试为“额外身份被拒绝”，新增test_order_tool_rejects_invalid_schema_before_query，六项实例覆盖缺字段、数字、None、空字符串、超长值和额外user_id，均在数据库查询前抛ValidationError。订单服务文件48项测试通过，-W error无警告；无外部模型调用。tools/orders.py调用关系不变，尚未接Agent。下一步由开发者实现统一工具执行入口，转换输入校验错误与未知工具名；不应把业务执行中出现的所有ValidationError都归为输入错误。

## 2026-11-02｜统一工具执行入口已验证

开发者新增 tools/executor.py 的 execute_tool：按服务端工具注册表查找名称，未知名称返回UNKNOWN_TOOL；通过get_input_schema().model_validate单独校验输入，失败返回INVALID_ARGUMENT；invoke在该捕获块之外，避免将执行内部的数据校验错误误归为输入错误。只支持当前字典参数/字典结果的项目工具，尚未实现完整模型ToolCall/ToolMessage协议或Agent循环。

```mermaid
flowchart TD
    CALL["独立调用 execute_tool：工具表、名称、参数"] --> LOOKUP{"名称已注册？"}
    LOOKUP -->|否| UNKNOWN["UNKNOWN_TOOL"]
    LOOKUP -->|是| VALID{"输入schema校验通过？"}
    VALID -->|否| INVALID["INVALID_ARGUMENT"]
    VALID -->|是| INVOKE["selected.invoke"]
    INVOKE --> ORDER["lookup_order：可信身份 → get_order_for_user"]
    ORDER --> RESULT["成功数据 / ORDER_NOT_FOUND / TEMPORARILY_UNAVAILABLE"]
```

新增源码函数清单：tools/executor.py — execute_tool。新增测试函数：test_tool_executor_dispatches_real_order_tool、test_tool_executor_unknown_tool_does_not_invoke_registered_tool、test_tool_executor_converts_input_errors_before_invocation、test_tool_executor_does_not_misclassify_internal_validation_error。六项实例覆盖真实订单工具派发、越权、未知工具、三个输入错误和内部ValidationError传播。订单服务测试文件54项通过，-W error无警告；格式与代码检查通过，无模型调用。今日政策工具尚未实现，今日范围尚未完成。

## 2026-11-02｜政策工具初稿（待验收）

开发者实际创建tools/policy.py（使用单数文件名，沿用该路径），新增build_policy_tools及内部search_order_policies。参数由OrderPolicySearchInput声明，仅order_id/item_id/query；可信user_id/as_of/session/embedding_client由工厂绑定。调用关系：独立tool.invoke → get_order_answer_context归属/商品检查 → search_semantic_policy_chunks（类别取自商品）→候选证据。未接Agent/HTTP。

初稿审查：调用顺序与类别来源正确，但当前直接返回list[SemanticPolicySearchHit]，与dict注解及统一ok/data协议不符；空白字段未统一拒绝、订单商品编号未strip、归属及检索异常尚未转换。模拟空检索实测返回裸列表[]，工具参数只有order_id/item_id/query；无真实数据库或模型调用。需由开发者补结果序列化和输入/错误边界，尚未验收。助手仅整理本轮代码格式。源码函数清单新增：tools/policy.py — build_policy_tools、search_order_policies。

### 政策工具基本结果验证通过（身份编号去空白与异常处理待补）

开发者已增加三个字段的空白拒绝、统一ok/data返回及SemanticPolicySearchHit的JSON序列化；空结果按成功空列表处理。助手复用tests/services/policies/test_semantic_retriever.py的engine/client fixture，新增test_policy_tool_blank_fields_do_not_access_context_or_search（三项）及test_policy_tool_uses_bound_date_and_item_category_and_serializes（两项）：覆盖空白字段不查询、绑定日期和商品类别、实际语义检索服务、JSON日期序列化、无候选不调用embedding。上下文查询使用模拟返回，完整归属链路尚待补测。该文件26项测试通过，-W error无警告，格式与代码检查通过，无真实模型调用。

仍需由开发者修正get_order_answer_context调用的order_id/item_id去空白；目前仅query去空白。LookupError、数据库/模型服务/索引校验错误尚未转换为工具错误。无新增运行函数，主调用图不变，政策工具尚未整体验收。


## 2026-11-02｜只读工具阶段离线验收完成

本节更新此前逐步审查中的待实现状态：订单工具、严格参数schema、政策工具和统一执行入口均已实现并验证。新增运行函数清单：tools/orders.py的build_order_tools、lookup_order；tools/policy.py的build_policy_tools、search_order_policies；tools/executor.py的execute_tool。新增输入类OrderLookupInput、OrderPolicySearchInput不定义额外函数，tools/__init__.py仅声明包。

```mermaid
flowchart TD
    ENTRY["execute_tool：服务端工具注册表 + 名称 + 参数"] --> SCHEMA["未知工具/输入错误转换；校验通过后invoke"]
    SCHEMA --> ORDER["lookup_order：可信user_id"]
    ORDER --> SERVICE["get_order_for_user → OrderResponse"]
    SCHEMA --> POLICY["search_order_policies：可信user_id/as_of"]
    POLICY --> CONTEXT["get_order_answer_context：订单归属 + 商品关系"]
    CONTEXT --> CATEGORY["使用商品category"]
    CATEGORY --> SEARCH["search_semantic_policy_chunks：日期过滤 + 索引校验 + embedding + 排序"]
    SEARCH --> RESULT["ok/data：JSON证据列表，可为空"]
    CONTEXT --> DENIED["不可访问：ORDER_CONTEXT_NOT_FOUND；不检索"]
    SEARCH --> FAIL["数据库/模型服务/ValueError：RETRIEVAL_UNAVAILABLE"]
```

助手新增验证函数test_policy_tool_executor_real_ownership_and_retrieval、test_policy_tool_denied_context_never_embeds、test_policy_tool_executor_rejects_untrusted_context_and_bad_input、test_policy_tool_failures_are_safe_and_never_call_chat及其内部故障注入辅助函数fail_context。均属测试，不进入业务运行图。复用既有临时库和answer_clients fixture，验证真实归属、两个商品类别、编号去空白、严格拒绝身份/日期/类别覆盖、模型/索引/数据库故障，不调用对话模型。

完整离线回归384项通过，-W error无警告；全后端Ruff通过，61个Python文件格式检查通过。本轮无真实模型调用，不以离线结果宣称实际模型选工具已验证。工具未接入HTTP、Agent或会话；仅支持项目内dict参数/结果，尚非完整ToolCall/ToolMessage执行器。工具不接管事务；数据库故障后由外层处理Session。检索失败错误未细分可重试性，不能自动无限重试。运行与目录职责不因新增工具而复制核心业务逻辑。


## 2026-11-02｜政策目录重组

用户明确要求按领域整理平铺目录。助手移动七个政策服务及对应七个测试模块至policies子目录，文件名去掉重复policy前缀，policy_retriever改为lexical_retriever；原函数与模型不变，业务代码与测试仅更新导入。新增services/policies/__init__.py仅声明包，无函数，不调用核心服务。无新增业务或测试函数；函数清单上方已按新路径更新。

修改引用覆盖API、CLI、固定回答服务、政策工具和测试；原HTTP路径与CLI模块入口不变。不保留旧Python导入兼容层，本仓库外若手工使用旧模块名需要更新。本次不修改数据库或调用真实模型。该段覆盖前文“保留原目录”的历史说明；最终验证结果见当日进度。

## 2026-11-03｜Agent状态声明完成，模型节点待实现

开发者新增agent/state.py的AgentState类，声明messages的add_messages合并规则、model_calls/tool_calls计数、stop_reason。助手验证模块导入、类型字段及两个消息的合并行为，并通过该文件Ruff与格式检查；无外部模型调用，无新增业务函数。AgentState仅声明状态，不调用既有核心函数，不表示图已可运行，当前HTTP/CLI仍使用原固定链路。

接下来计划由开发者新增agent/nodes.py中的build_model_node及内部call_model：绑定既有工具schema并根据当前messages调用对话模型，返回消息增量与调用次数；模型工具请求执行由后续工具节点复用execute_tool。该计划尚未实现，不画入现有运行调用图。

## 2026-11-03｜模型节点初稿审查（预算判断待修正）

开发者新增agent/nodes.py的build_model_node及内部call_model。工厂通过chat_client.bind_tools绑定工具schema；预期模型节点读取消息，调用模型，返回单条消息增量、model_calls+1和running/completed状态。模型节点不执行工具、不直接查数据库。尚未组装Graph或接入HTTP/CLI。

初稿实测：用普通dict状态和模拟客户端调用，预算判断state.model_calls > max.model_calls触发AttributeError，未进入invoke。应使用state["model_calls"] >= max_model_calls，在达到而非超过上限时阻止请求。助手仅整理格式，预算逻辑由开发者修正；模型异常及响应校验尚待实现。本轮无真实模型调用，尚未通过节点行为验收。新增源码函数清单：agent/nodes.py — build_model_node、call_model（内部函数）。

### 模型节点成功路径与预算边界通过

开发者已修正state字典取值及>=预算判断。助手新增backend/tests/agent/test_nodes.py，调用build_model_node并模拟bind_tools/invoke，新增测试函数test_model_node_returns_message_increment_without_mutating_state、test_model_node_exhausted_budget_never_invokes_model，共六项实例覆盖直接回答/工具请求、首轮/最后允许一次调用、达到/超过上限不invoke、返回消息增量且不原地修改状态。六项通过，-W error无警告，相关Ruff与格式检查通过；无数据库或真实模型调用。

运行函数无新增，尚未接图。剩余：异常及非AIMessage/invalid_tool_calls处理；失败请求也需计入调用预算，以尝试次数限制运行。尚未实现的模型/工具循环不计为可用入口。新增测试文件只做验证、不调用核心业务服务，工具执行仍由后续节点复用execute_tool。

### 模型节点失败尝试计数与响应校验通过

开发者在call_model计入调用尝试next_calls，捕获OpenAIError/TimeoutError返回failed；非AIMessage或存在invalid_tool_calls也终止且不追加异常消息。助手新增测试函数test_model_node_failed_attempt_counts_and_does_not_append_response，五项实例覆盖超时、API超时子类、上游服务异常、错误消息类型、无法解析的工具请求。节点测试共11项通过，-W error无警告，相关Ruff与格式检查通过；无真实模型调用。函数调用关系不变，仍未组装Graph。工具节点及条件路由待开发；模型预算配置和初始状态运行边界将在组装入口一并校验。

## 2026-11-03｜工具节点初稿（返回状态待补）

开发者新增agent/nodes.py的build_tool_node及内部call_tools：读取最后AIMessage、检查整批工具预算、循环调用execute_tool并构造对应tool_call_id的ToolMessage。助手整理导入与缩进，仅使用模拟execute_tool试跑一次合法调用：执行器已调用一次，但call_tools末尾未返回状态，实际返回None，工具结果及计数无法写回图状态，尚未通过节点验收。

新增源码函数清单：agent/nodes.py — build_tool_node、call_tools（内部函数）。预期调用关系：call_tools → execute_tool → 已注册工具 → services；ToolMessage加入messages后才交回模型节点。尚未组装Graph，调用编号/输出大小/故障中断及空消息防护待补；本轮无真实工具或模型服务调用。

### 工具节点返回状态与整批预算验证通过

开发者已补齐call_tools返回messages增量、tool_calls按本批派发次数增加和running状态。助手在既有test_nodes.py新增test_tool_node_returns_matching_messages_and_counts_batch、test_tool_node_rejects_entire_batch_over_budget、test_tool_node_rejects_messages_without_tool_requests，共六项实例：两请求的消息ID/JSON结果/顺序、成功和未知工具结果、刚好用完预算、整批超额零执行、无工具请求消息拒绝、不修改原状态。节点文件共17项通过，-W error无警告，相关代码和格式检查通过，无真实工具/模型调用。

仍需处理空messages、缺失/空白/重复工具调用ID、执行异常、基础设施错误终止和输出大小；完整图尚未组装。函数调用图不变，本次仅补已有函数的行为验证。

### 工具节点整批编号预检通过

开发者在读取末条消息前检查空列表，并在执行任何工具前拒绝缺失、非字符串、空白或重复的调用ID。助手新增test_tool_node_rejects_invalid_call_ids_before_any_execution，六项实例覆盖空历史及畸形ID，第二个请求不合法时第一个也不执行，证明预检发生在整批派发前。节点测试共23项通过，-W error无警告，相关Ruff与格式检查通过；无真实外部调用。仅增加验证，无新增运行函数，Graph尚未组装。

### 工具节点失败中断与结果大小限制完成（用户授权助手直接修改）

用户明确要求本次由助手直接完成，并持续授权已讲清方案的同类局部修正；已记录AGENTS.md。助手修改agent/nodes.py的call_tools，不新增运行函数：派发前增加尝试次数，捕获执行/序列化异常并failed终止；非字典结果拒绝；JSON禁止NaN；单条序列化结果按max_output_chars字符限制，超限丢弃该条而不截断JSON；基础设施错误TEMPORARILY_UNAVAILABLE/RETRIEVAL_UNAVAILABLE保留对应ToolMessage后终止余下请求。之前成功的结果与尝试次数保留，原state不变，不输出内部异常详情。

```mermaid
flowchart TD
    PRE["整批编号与预算预检"] --> COUNT["计入本次派发尝试"]
    COUNT --> EXEC["execute_tool → 字典结果 → JSON序列化"]
    EXEC -->|异常/超长| FAIL["保留先前结果和已尝试次数，failed终止"]
    EXEC -->|结果合规| MSG["追加对应ID的ToolMessage"]
    MSG --> INFRA{"基础设施错误？"}
    INFRA -->|是| FAIL
    INFRA -->|否| MORE["继续下一请求；整批成功后返回running"]
```

新增测试函数：test_tool_node_stops_on_unusable_output_and_counts_attempt、test_tool_node_infrastructure_failure_returns_error_and_skips_remaining_tools、test_tool_node_output_size_boundary_is_serialized_character_count、test_tool_node_retains_completed_results_before_later_failure。十二项实例覆盖执行异常/不可序列化/NaN/非字典/超长、两个基础设施错误、中文JSON字符长度刚好/超出边界、后续失败保留此前结果。节点测试共35项通过，-W error无警告，相关Ruff及格式检查通过，无数据库/外部模型调用。

当前失败状态可能保留未获结果的工具请求，后续Graph必须直接END，不得继续发模型或将其当成可恢复会话；当前不支持持久化恢复。字符限制是单条JSON输出限制，不是总token预算或总历史大小限制。仅节点独立验证完成，Graph及条件路由尚未实现，今日范围尚未完成。


## 2026-11-03｜有界Agent图离线验收完成

前文逐步记录中的“尚未组装Graph”属于当时状态，以本节及页首为当前状态。开发者实现route_after_model、route_after_tools及build_agent_graph基本连接。助手按局部修正持续授权补正整数配置/工具实例/重名校验、内部validate_start、模型预算耗尽后不再派发工具，以及随预算调整图recursion_limit；新增agent/__init__.py只声明包。编排逻辑复用已验收的节点工厂和工具，不复制业务查询或检索。

```mermaid
flowchart TD
    START["graph.invoke：新运行消息 + 零计数 + running"] --> CHECK["validate_start：初始状态校验"]
    CHECK --> MODEL["call_model：预算 → invoke → AIMessage/请求校验 → 计数"]
    MODEL --> MR{"route_after_model"}
    MR -->|completed/failed/budget_exhausted| END["END：返回状态"]
    MR -->|running| TOOLS["call_tools：模型预算、整批ID与工具预算预检"]
    TOOLS --> EX["execute_tool → 可信身份绑定工具 → services"]
    EX --> TM["大小/错误检查 → ToolMessage增量与尝试计数"]
    TM --> TR{"route_after_tools"}
    TR -->|failed/budget_exhausted| END
    TR -->|running| MODEL
```

完整运行入口：agent.graph.build_agent_graph(...).invoke(state)，配置和示例见README。只接受零计数的新运行，不支持恢复、checkpointer、HTTP会话；失败末态不得直接续传模型。配置在bind_tools前校验，初始状态在invoke模型前校验。输出为AgentState消息历史及终止原因，尚未接PolicyAnswer解析器，不能将completed等同于引用有效或回答正确。

新增测试文件backend/tests/agent/test_graph.py调用build_agent_graph、build_order_tools、build_policy_tools、seed_demo_data/build_engine/init_db等既有函数，用临时数据库及模拟embedding/chat客户端；无真实付费请求。测试辅助函数graph_context、initial_state、tool_request，以及两个局部reply提供模拟响应。新增测试函数：test_graph_selects_next_tool_from_prior_result_and_finishes、test_graph_direct_answer_skips_tools、test_graph_tool_error_is_visible_to_next_model_call、test_graph_model_budget_stops_before_unusable_last_tool_batch、test_graph_tool_budget_stops_without_partial_batch、test_graph_model_failure_ends_without_more_requests、test_graph_tool_failure_never_sends_incomplete_history_to_model、test_graph_rejects_invalid_limits_before_binding_model、test_graph_rejects_duplicate_tool_names、test_graph_rejects_invalid_initial_state_before_model_call、test_graph_large_model_budget_uses_matching_graph_step_limit。纯验证函数不混入运行图。

Agent相关67项、完整后端451项测试通过，-W error无警告；全后端Ruff通过，68个Python文件格式检查通过。已验证模拟模型根据第一次订单结果决定下一政策工具；不是实际模型工具选择评测。本轮无真实模型调用、不写演示库。工具输出上限是单条字符限制，通用failed暂不提供详细事件原因；当前无结构化最终回答校验、真实Agent试跑、会话或记忆。当天有界循环范围收口。
