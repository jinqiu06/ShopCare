# 模拟政策资料与读取模块任务

## 数据边界

`data/policies/` 的八份 JSON 是为本项目自编的模拟规则，不代表真实商家承诺或法律要求。六份当前版本、两份历史版本，类别与模拟商品项中的“数码”“配件”一致。

固定样例的签收日期包含 2026-10-30。后续演示与评测明确使用参照日期 2026-11-02，不以机器当天时间推算资格；真实当前时间的行为以后单独设计。

## 文件字段

- policy_id：政策稳定标识；同一政策可以有多个版本。
- version：版本，例如 v1、v2。
- title：标题。
- category：适用商品类别。
- effective_from：生效日期，含当日。
- effective_until：失效日期，不含当日；null 表示尚未规定结束日期。
- content：完整政策正文，保留原文以便追溯。

区间统一为 `[effective_from, effective_until)`。例如旧版于 2026-10-01 失效，新版同日生效，两版不会同时有效。

唯一键是 `(policy_id, version)`，不能只按 policy_id 去重。日期范围必须有效，字符串字段不能为空或仅含空白，不接受未声明字段。

## 开发者负责的核心实现

按整理后的目录，在开始实现时创建 `schemas/policies.py` 与 `services/policy_loader.py`；当前尚未创建。

1. 用 Pydantic BaseModel 定义 PolicyDocument，日期用 date，正文保留原文。
2. load_policy(path: Path) -> PolicyDocument：UTF-8 读取并校验一个 JSON 文件。
3. load_policies(directory: Path) -> list[PolicyDocument]：按文件名排序读取顶层 JSON，并检查唯一键冲突。
4. 单文件损坏、字段缺失或不合法、重复版本应失败并指出文件或标识；不能静默跳过。
5. 缺失目录抛 FileNotFoundError；目录存在但没有 JSON 文件时返回空列表。
6. 本阶段只加载全部政策，不过滤生效时间、不切分、不入库、不调用模型。有效性过滤在检索阶段基于显式参照日期实现。

## 助手负责的验证

正常读取八份样例、中文和日期解析、历史版本保留、稳定顺序、缺失与空目录、非法 JSON、缺失字段、空白字段、非法日期区间、未声明字段和重复唯一键。
