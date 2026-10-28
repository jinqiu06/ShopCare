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

已实现 `schemas/policy_models.py` 与 `services/policy_loader.py`，文件加载及以下校验已通过测试。

1. 用 Pydantic BaseModel 定义 PolicyDocument，日期用 date，正文保留原文。
2. load_policy(path: Path) -> PolicyDocument：UTF-8 读取并校验一个 JSON 文件。
3. load_policies(directory: Path) -> list[PolicyDocument]：按文件名排序读取顶层 JSON，并检查唯一键冲突。
4. 单文件损坏、字段缺失或不合法、重复版本应失败并指出文件或标识；不能静默跳过。
5. 缺失目录抛 FileNotFoundError；目录存在但没有 JSON 文件时返回空列表。
6. 本阶段只加载全部政策，不过滤生效时间、不切分、不入库、不调用模型。有效性过滤在检索阶段基于显式参照日期实现。

## 助手负责的验证

正常读取八份样例、中文和日期解析、历史版本保留、稳定顺序、缺失与空目录、非法 JSON、缺失字段、空白字段、非法日期区间、未声明字段和重复唯一键。


## 已实现：适用政策过滤

已在 `services/policy_filter.py` 中实现两个纯函数，不做文件读取、数据库访问或模型调用：

- is_policy_effective(policy, as_of)：有效区间为 effective_from <= as_of < effective_until；无结束日期只检查开始日期。
- filter_applicable_policies(policies, category, as_of)：同时按精确类别和有效日期筛选，保留输入顺序；不改动原列表，也不删除历史版本。

日期由调用者显式提供，不从 date.today() 读取。版本号不代表生效状态，不能只挑字符串最大的 version。当前只判断政策是否适用，不判断订单最终退换资格。

样例参照 2026-11-02：八份政策中六份有效，数码与配件各三份；数码旧版在 2026-10-01 当天已失效。


## 下一步：可追溯的切分基线

在 schemas/policy_models.py 定义 PolicyChunk，在 services/policy_chunker.py 实现 split_policy。第一版按 Python 字符位置切片，不按字节或 token 计数。

默认 chunk_size=300、overlap=50；要求 chunk_size 为正整数且 0 <= overlap < chunk_size。片段位置为 [start_char, end_char)，text 必须等于原文相应切片，不 strip 或改写原文。

每片保留 policy_id、version、title、category、effective_from、effective_until，以及 chunk_id、text、start_char、end_char。chunk_id 由政策 ID、版本和从 0 开始的片段序号组成；同一政策版本的正文后续按不可变处理，若更新必须更换版本并重建索引。

每个片段最长 chunk_size；相邻窗口有 overlap 个字符的重叠。最后一个窗口覆盖原文尾部后立即停止，不再生成只包含上一个尾部的重复片段。短政策保留为单片，不为凑数量强制切碎。固定窗口只是第一版基线，后续按评测考虑段落边界切分。
