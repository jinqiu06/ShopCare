# 词法与语义检索对照

运行时间（UTC）：2026-10-30T09:27:15.393378+00:00
模型：text-embedding-v4；维度：1024；top_k=3。
问题向量调用 10 次；不重建政策向量，不修改数据库。

同一本地数据库、同一组开发问题、相同类别和日期过滤，直接调用服务层；未经过 HTTP。
语义检索未设置相关性阈值，因此无关问题也可能返回候选。

| 模式 | 正例 Hit@1 | 正例 Hit@3 | 负例正确空返回 |
|---|---|---|---|
| lexical | 8/9 | 8/9 | 1/1 |
| semantic | 9/9 | 9/9 | 0/1 |

| 案例 | 问题 | 目标 | 词法排名 | 语义排名 | 语义首条（分数） |
|---|---|---|---|---|---|
| Q01 | 耳机单侧无声换货 | digital-exchange:v2 | 1 | 1 | digital-exchange:v2 (0.4974) |
| Q02 | 数码未拆封退货 | digital-return:v2 | 1 | 1 | digital-return:v2 (0.7579) |
| Q03 | 耳机故障超期人工处理 | digital-after-window:v1 | 1 | 1 | digital-after-window:v1 (0.6196) |
| Q04 | 配件包装齐全未使用退货 | accessory-return:v1 | 1 | 1 | accessory-return:v1 (0.6951) |
| Q05 | 配件破损规格不符换货 | accessory-exchange:v1 | 1 | 1 | accessory-exchange:v1 (0.7989) |
| Q06 | 配件超期签收记录缺失 | accessory-after-window:v1 | 1 | 1 | accessory-after-window:v1 (0.7404) |
| Q07 | 耳机质量问题换货 | digital-exchange:v1 | 1 | 1 | digital-exchange:v1 (0.6140) |
| Q08 | 耳机质量问题换货 | digital-exchange:v2 | 1 | 1 | digital-exchange:v2 (0.6502) |
| Q09 | 左边听不见了想换一副 | digital-exchange:v2 | 未命中 | 1 | digital-exchange:v2 (0.3149) |
| Q10 | zzzzuniquenomatch | 应为空 | 未命中 | 未命中 | digital-after-window:v1 (0.3132) |

排名对负例不适用，负例依据是否为空单独统计。
这是十个人工开发案例，不是独立保留集，不代表真实业务准确率。
两种分数含义不同，不能直接比较；耗时是单次顺序运行，不能作为性能基准。
逐题完整候选、分数、耗时和数据指纹见同名 JSON 文件。
本轮不依据这十题调相关性阈值或增加混合排序。
