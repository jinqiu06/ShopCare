# 政策片段入库设计

状态：切分、数据表、category 索引、替换入库服务及导入命令均已实现并通过测试。

## 第一版存储

在 db/models.py 添加 PolicyChunkRecord，表名 policy_chunks。与 schemas/policy_models.py 中的 Pydantic PolicyChunk 区分：前者负责数据库表，后者负责片段数据传递。

| 列 | 类型 | 约束 |
| --- | --- | --- |
| chunk_id | String(255) | 主键 |
| policy_id | String(100) | 非空、索引 |
| version | String(50) | 非空 |
| title | String(200) | 非空 |
| category | String(50) | 非空、索引 |
| effective_from | Date | 非空 |
| effective_until | Date | 可空 |
| text | Text | 非空 |
| start_char | Integer | 非空且 >= 0 |
| end_char | Integer | 非空且 > start_char |

片段可跨用户共用，不放 user_id。本阶段政策原文以仓库中的 JSON 为权威来源，通过 policy_id + version 追溯；尚不提供独立的全文数据库副本。

## 重建语义

仅按 chunk_id 跳过已存在记录，不足以处理修改 chunk_size 后的重新切分：如果新片段数量减少，旧尾部会残留。因此第一版按一个政策版本整体替换，不做按行跳过。

建议在 services/policy_store.py 实现：

replace_policy_chunks(session, policy, chunks) -> int

- 验证所有片段与 policy 的 ID、版本、标题、类别和生效区间一致；chunk_id 不重复，片段位置和 text 对应原文切片。
- 对非空政策，空 chunks 应拒绝，避免误删有效数据。
- 校验全部通过后，仅删除该 policy_id + version 的旧片段，再插入新片段并 flush。
- 不删除其他政策或其他版本；不在函数内部 commit，由调用方管理事务。
- 返回本次写入片段数。重复调用后数据库状态一致，即幂等；不要求第二次返回 0。
- 若替换中途失败，调用方回滚必须能恢复原来的整组片段。
- 政策正文在同一版本下按不可变处理；更新正文应换版本。切分参数改变需要重建该版本的全部片段与未来相关检索索引。

## 验证重点

正常写入、重复替换、窗口变大后旧尾部清理、其他版本不受影响、错误来源或切片被拒绝、失败回滚、调用方未提交不落盘。

当前没有独立政策父表，不要定义指向尚不存在政策表的外键。后续需要全文管理时再单独设计。


## 导入命令

从项目根目录运行：

```powershell
.venv/Scripts/python -m shopcare.scripts.ingest_policies
```

可指定 --policy-dir、--database-url、--chunk-size、--overlap。默认使用 data/policies、配置中的 SQLite URL、300 字符窗口和 50 字符重叠。

当前八份样例都短于默认窗口，导入结果为 policies_processed=8、chunks_written=8；重复运行仍报告重写八片，但数据库总数保持八片。变更窗口时，对每份政策版本整体替换。导入不提前过滤历史版本，生效日期与类别在查询阶段限制。

目录中移除一个政策文件，不会自动删除数据库中的该政策；当前仅替换本批提供的政策版本，不是整个政策库的镜像同步。固定版本正文不可变，更新需更换版本。
