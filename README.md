# ShopCare

面向电商售后场景的 Agent 后端项目。当前已完成订单查询、用户范围过滤、商品项查询、分页、模拟数据初始化及自动测试；政策检索、模型工具调用与记忆机制按阶段开发中。

## 当前功能

- 基于 FastAPI 提供订单列表、订单详情和独立商品项接口。
- 订单查询同时限制订单归属，其他用户订单与不存在订单统一返回 404。
- 列表按订单 ID 稳定排序，支持 limit / offset 分页和参数校验。
- SQLAlchemy 模型包含外键、状态、非负金额和正数数量约束；SQLite 连接显式开启外键检查。
- 提供可重复运行的模拟数据命令：2 名用户、20 笔订单、40 个商品项；已有记录不覆盖。
- Pydantic 配置支持根目录 `.env` 和环境变量覆盖，缺少模型 Key 不影响订单查询。
- 72 项离线测试覆盖配置、数据库、事务回滚、初始化、服务与 API。

## 环境与安装

当前支持 Python 3.13–3.14，本机验证环境为 Windows / Python 3.13；依赖快照基于该环境。无需前端或 Node.js。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock.txt
.venv/Scripts/python -m pip install --no-deps -e .
Copy-Item .env.example .env
```

如已有 `.env`，不要覆盖本机配置。macOS / Linux 使用 `.venv/bin/python` 替换 `.venv/Scripts/python`。

所有命令从项目根目录执行。`.env` 与 SQLite 相对路径均以当前工作目录为基准。模型配置尚未用于真实模型调用，空值即可运行当前功能。

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
│   │   ├── orders.py         # 订单路由与 HTTP 参数
│   │   ├── dependencies.py   # 请求级 Session、演示身份
│   │   └── errors.py         # HTTP 错误响应
│   ├── core/config.py        # 环境配置
│   ├── db/
│   │   ├── base.py           # ORM 基类
│   │   ├── session.py        # Engine、外键配置、显式建表
│   │   └── models.py         # 用户、订单、商品项
│   ├── schemas/orders.py     # 接口响应模型
│   ├── services/orders.py    # 可被 API / 后续工具复用的查询逻辑
│   └── scripts/seed.py       # 模拟数据与初始化命令
└── tests/
    ├── api/                  # 健康检查、接口与错误
    ├── core/                 # 配置
    ├── db/                   # 数据约束、回滚与初始化
    └── services/             # 用户隔离、商品项和分页
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

`data/policies/` 包含八份自编 JSON 政策，覆盖数码、配件及新旧版本，仅用于项目演示，不代表真实商家承诺或法律要求。当前尚未实现政策加载、检索与模型回答。

模拟订单采用固定 UTC 时间约定，包含 2026-10-30 的签收记录。演示政策判断拟使用固定参照日期 2026-11-02；当前 SQLite 时间响应不带时区标记。

后续依次实现政策加载与切分、检索基线、模型回答、有界工具循环、人工确认、会话恢复、长期记忆和固定案例评测。尚未实现的能力不计入当前功能。

- [需求与接口约定](docs/requirements.md)
- [数据模型](docs/data-model.md)
- [政策资料规范](docs/policy-data.md)
- [开发规划](docs/开发规划.md)
- [进度记录](docs/progress.md)

本地 `.env`、虚拟环境、数据库、日志和真实用户数据不提交 Git；只提交代码、依赖、文档与公开模拟资料。
