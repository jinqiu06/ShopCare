# 词法检索初始基线

八份自编政策、十个人工固定问题，通过实际 HTTP 路由在独立临时数据库运行。
无模型、embedding 或真实商家数据；不同案例的参照日期见输入文件。

- 有目标政策问题：9 个。
- Hit@1：8/9。
- Hit@3：8/9。
- 预期无结果案例正确返回空列表：1/1。

Hit@k 的分母仅为有目标政策的案例，匹配同时要求 policy_id 和 version。
每个案例只标注一个目标政策，因此这里记录 Hit@k，不宣称完整相关性 Recall。

| ID | 问题 | 目标政策 | 前三条（分数） | 目标位置 |
| --- | --- | --- | --- | --- |
| Q01 | 耳机单侧无声换货 | digital-exchange:v2 | digital-exchange:v2 (6), digital-after-window:v1 (1) | 1 |
| Q02 | 数码未拆封退货 | digital-return:v2 | digital-return:v2 (4), digital-after-window:v1 (1), digital-exchange:v2 (1) | 1 |
| Q03 | 耳机故障超期人工处理 | digital-after-window:v1 | digital-after-window:v1 (4), digital-exchange:v2 (3) | 1 |
| Q04 | 配件包装齐全未使用退货 | accessory-return:v1 | accessory-return:v1 (5), accessory-after-window:v1 (3), accessory-exchange:v1 (1) | 1 |
| Q05 | 配件破损规格不符换货 | accessory-exchange:v1 | accessory-exchange:v1 (6), accessory-after-window:v1 (1), accessory-return:v1 (1) | 1 |
| Q06 | 配件超期签收记录缺失 | accessory-after-window:v1 | accessory-after-window:v1 (7), accessory-exchange:v1 (2), accessory-return:v1 (2) | 1 |
| Q07 | 耳机质量问题换货 | digital-exchange:v1 | digital-exchange:v1 (5), digital-return:v1 (3) | 1 |
| Q08 | 耳机质量问题换货 | digital-exchange:v2 | digital-exchange:v2 (6), digital-return:v2 (3), digital-after-window:v1 (2) | 1 |
| Q09 | 左边听不见了想换一副 | digital-exchange:v2 | 空列表 | 未命中 / 不适用 |
| Q10 | zzzzuniquenomatch | 应返回空列表 | 空列表 | 未命中 / 不适用 |

## 解释与局限

- 本组案例是开发用小样本，其中多条用词接近政策原文，不是独立保留测试集。
- 双字/英文特征只支持字面匹配，不能理解同义改写、否定或完整业务资格。
- Q09 故意使用口语改写；结果应原样保留，不能修改预期来美化指标。
- 高分只代表命中特征多，不是置信度；相似但不适用的问题仍可能返回片段。
- 日期/类别正确属于程序约束，不等于最终售后结论正确。
- 下一轮应补独立问题、歧义/否定与无答案案例，再比较检索方案。

## 复现

```powershell
.venv/Scripts/python -m shopcare.scripts.evaluate_retrieval
```
