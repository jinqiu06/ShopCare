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
