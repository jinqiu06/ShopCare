# 单次回答真实试跑记录

2026-10-31；固定模拟用户 U001、订单 O004、商品 O004-I01，参照日期 2026-11-02。
模型 qwen-plus；embedding text-embedding-v4 / 1024 维。每次最多一次问题向量和一次对话调用，不修改数据库。

| 样例 | 回答状态 | 总耗时 ms | 对话 tokens | 人工验收 |
|---|---|---|---|---|
| answer-demo.json | answered | 3592.65 | 986 | 未通过：对未实现的草稿功能作承诺；故障定性过强 |
| answer-demo-revised.json | answered | 4889.68 | 1138 | 未通过：新增边界规则后仍承诺生成草稿 |
| answer-demo-clarification.json | answered | 6507.17 | 1203 | 未通过：应澄清却为 answered，日期推理自相矛盾并承诺草稿 |
| answer-demo-unrelated.json | insufficient_evidence | 2985.74 | 1049 | 本次通过：无关天气问题返回 insufficient_evidence，无引用 |

四次试跑的对话总用量为 4376 tokens；不包含 embedding 用量。
所有已保存回答均通过现有 JSON/引用来源校验；这不能证明事实推理、能力边界或状态选择正确。
不是固定验收集准确率。保留首轮与修订原始 JSON，不以重复请求挑选最好答案。
本轮内容验收未完成；暂停付费重试，先改进规则和状态示例。完整链路仍无 HTTP 回答接口。


## 第二次修订复测（仍未最终通过）

- answer-demo-final.json：6413.08ms，1398 对话 tokens；不再承诺代为生成草稿，且声明故障为用户自述。但仍额外生成具体截止日期，并对包装/配件条件作无充分证据的判断。
- answer-demo-clarification-final.json：4437.56ms，1298 对话 tokens；状态改为 needs_clarification，正文正确询问故障、说明未创建申请。但 limitations 仍出现“已过15日”又“尚未超出期限”的矛盾。
- 文件名 final 仅表示第二次修订保存路径，不代表最终验收通过。六次真实试跑总对话用量 7072 tokens，不含 embedding；未继续重复请求挑选最好结果。
- 后续先由核心代码计算签收后的日期差并提供明确事实，减少模型日期运算；该做法也不能自动保证生成文本无误，仍需检查正文与 limitations。


## 接入确定性日期差后的单例复测

- 原始记录：answer-demo-computed-days.json。相同模拟订单、商品、参照日期与模糊问题“耳机坏了”。
- 总耗时3511.02ms；对话输入1206/输出131/总1337 tokens，不含 embedding。
- 本例人工检查通过：needs_clarification，询问故障，说明未创建申请；limitations 正确使用 days_since_delivery=3，没有日期矛盾，未直接认定符合换货条件。
- 七次真实试跑对话总 usage 8409 tokens，不含 embedding。不将修订过程当作七题独立准确率。
- 这是单例回归改善；未对最终提示词重新跑明确故障、天气或独立保留问题，不能宣称当前版本所有真实场景均正确。引用格式校验不证明每句话有依据，模型稳定性与事实一致性仍有限制。
