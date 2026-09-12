# V3 真实模型 30 题：Base 对照

> 主模型 `deepseek-v4-pro`；双语义 Judge `deepseek-v4-flash`；产品证据 Judge 两组均开启。

| 指标 | Base | V3 |
|---|---:|---:|
| Judge 1 Precision | 1.0 | 1.0 |
| Judge 1 Recall | 1.0 | 1.0 |
| Judge 1 F1 | 1.0 | 1.0 |
| Judge 2 Precision | 1.0 | 1.0 |
| Judge 2 Recall | 1.0 | 1.0 |
| Judge 2 F1 | 1.0 | 1.0 |
| Judge 1 完全正确样本 | 1 | 1 |
| Judge 2 完全正确样本 | 1 | 1 |
| 双 Judge 一致样本 | 1/30 | 1/30 |
| 平均延迟 ms | 27473.6 | 92911.59 |
| 主链路模型调用 | 1 | 8 |
| 工具调用 | 0 | 3 |

> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个有标准答案的 Judge 裁决。
> execution_completed=true