# 第一阶段数据模型与实现任务

状态：核心模型、外键与初始化已实现并通过测试；服务启动不自动创建演示数据。

## 范围与约定

先实现 User、Order、OrderItem 三张表。物流和售后申请按后续功能需要再增加，不预建整套系统。

- 金额用整数“分”，不要用浮点数存储金额。
- 演示版本暂不含运费和优惠，订单总额等于商品单价乘数量之和；由业务服务在同一事务内校验。
- 时间以 UTC 约定处理；API 输出明确时区。SQLite 的日期时间行为需要在实现时验证，不假设类型参数会自动完成时区转换。
- ID 使用可重复的模拟字符串，便于种子数据与验收案例引用。

## 表设计

| 表 | 字段 | 含义与约束 |
| --- | --- | --- |
| users | id | 字符串主键，例如 U001 |
| users | display_name | 非空显示名称，不用于认证 |
| orders | id | 字符串主键，例如 O001 |
| orders | user_id | 非空外键，指向 users.id；建立查询索引 |
| orders | status | created / paid / shipped / delivered / cancelled |
| orders | total_amount_cents | 非空整数，数据库约束 >= 0 |
| orders | created_at | 创建时间，保存规则需一致 |
| orders | delivered_at | 可空签收时间 |
| order_items | id | 字符串主键，例如 I001 |
| order_items | order_id | 非空外键，指向 orders.id；建立查询索引 |
| order_items | product_name | 非空商品名称 |
| order_items | category | 非空商品类别，后续用于政策过滤 |
| order_items | quantity | 非空整数，数据库约束 > 0 |
| order_items | unit_price_cents | 非空整数，数据库约束 >= 0 |

一个用户对应多个订单，一个订单对应多个商品项。第一版先定义外键列，再按实际查询需要增加 ORM relationship，避免一次学习过多隐式加载行为。

## 数据库基础设施要求

现有模块：`shopcare/db/base.py`、`shopcare/db/session.py`、`shopcare/db/models.py`。

- 使用 SQLAlchemy 2 风格，采用 DeclarativeBase、Mapped、mapped_column。
- `build_engine(database_url)` 创建 Engine，URL 从配置传入，测试可使用独立数据库。
- SQLite 默认不保证每个连接都启用外键检查，需要明确启用并测试。
- 提供初始化入口，只有显式初始化时才创建目录与表；导入应用不应自动创建业务数据。
- Session 管理要明确事务提交、异常回滚和连接释放。
- 模型模块需在建表前被加载，否则 metadata 中可能没有目标表。
- 第一阶段从根目录运行，开发数据库 URL 约定为 `sqlite:///./data/shopcare.db`。将来变更工作目录策略时统一处理路径。

## 实现顺序

1. 配置增加 database_url；实现 Base 与可传入 URL 的 Engine 构造函数。
2. 实现三个 ORM 模型、外键、状态和数值约束。
3. 在独立临时数据库中初始化并插入一名用户、一笔订单和两个商品项，查询验证。
4. 实现可重复运行的模拟数据初始化：至少两名用户、20 笔订单，覆盖各订单状态；不重复插入。
5. 实现当前用户订单查询服务，验证其他用户的订单不可访问，再连接 HTTP 接口。

## 验收由助手协助完成

- 临时数据库独立于 data/shopcare.db；初始化可重复。
- 插入与查询有效；非法数量、负金额、非法状态与无效外键被拒绝。
- 事务失败不会留下部分数据，Session 使用后释放。
- 种子数据重复执行不增加订单数。
- 当前用户只能读取自己的订单；总额计算不使用浮点误差。

SQLite 内存库在多连接或 TestClient 下可能需要专门的连接池配置。初期优先使用临时文件数据库，避免把数据库生命周期问题混入业务学习。

## 11月11日：售后草稿设计（计划，尚未实现）

AfterSalesDraft是用户售后意向的持久化记录，不表示符合政策、已确认或已创建正式申请。今日只实现创建/归属查询/取消服务与离线验证，确认及正式申请后续单独设计。暂不关联Conversation，草稿归属直接绑定可信user_id与订单/商品，避免把业务记录生命周期绑死到聊天执行状态。

计划在既有backend/src/shopcare/db/models.py定义AfterSalesDraft，表名after_sales_drafts，不新增Python文件。字段：id(String50主键)、user_id(String50非空外键users.id且索引)、order_id(String50非空外键orders.id)、item_id(String50非空外键order_items.id)、request_type(String20，return/exchange)、reason(Text非空)、status(String20，pending/cancelled)、version(Integer>=1)、as_of(Date非空，可信业务参照日期)、created_at/updated_at/expires_at(DateTime非空，UTC无时区存储与现有会话约定一致)。统一在创建服务中显式赋初值，不使用数据库默认值悄悄补业务状态。

数据库约束仅request_type枚举、status枚举及version>=1；归属与商品是否属于该订单由服务结合已有查询验证，普通外键本身不能证明三者匹配。服务拒绝空白原因与无效过期时间；取消采用条件更新，禁止越权或不合法状态流转。具体资格校验在后续正式提交时依据明确业务规则重新验证，不能把模型自然语言回答作为可执行资格判定。

初始pending表示待用户确认，允许pending→cancelled；今天不实现submitted或确认接口，不声称有正式申请/幂等性。version初值1，取消时递增；expires_at用于拒绝过期草稿后续确认，具体有效期由创建服务明确设置。as_of用于演示业务日期，与真实UTC创建/过期时间区分。此设计不是当前已运行表，需待开发者实现后再更新状态和验证记录。

### AfterSalesDraft模型实施状态（11月11日）

开发者已写大部分字段，助手按本次明确授权补order_id与三个数据库CheckConstraint并整理布局。计划中的表类已存在，可由init_db在显式初始化的数据库创建；本次只操作测试临时库，没有给演示库建表。尚无创建/查询/取消服务或HTTP/Agent接线。数据库约束不校验用户-订单-商品的组合关系，不判售后政策资格，也不阻止空白原因；下一步创建服务负责前两项业务检查与空白原因拒绝，资格重校验留到正式提交设计。

数据库测试文件34项通过，包括本次新增11组草稿持久化和拒绝非法记录验证；无新增Python文件。创建服务初步约定24小时有效期（实际UTC时间），与模拟参照日期as_of分开，不由客户端任意设置；具体实现待开发者完成。

### 当前状态：草稿业务服务闭环已实现（11月11日）

上方“计划/尚未实现”为分步设计时的记录。当前AfterSalesDraft及services/after_sales.py的create_after_sales_draft/get_after_sales_draft_for_user/cancel_after_sales_draft已完成。创建固定24小时有效期，不接收客户端指定过期时间；查询按归属返回各状态/已过期草稿；取消按id/user_id/pending/版本条件更新，成功变cancelled并version+1，失败抛冲突，不commit。

仅临时库验证，未对演示库执行DDL或写草稿。状态仍仅pending/cancelled，没有正式申请、确认、幂等提交、HTTP或Agent工具。草稿服务25项与数据库34项通过。
