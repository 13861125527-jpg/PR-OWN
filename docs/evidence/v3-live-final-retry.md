# V3 真实模型 30 题：Base 对照

> 主模型 `deepseek-v4-pro`；双语义 Judge `deepseek-v4-flash`；产品证据 Judge 两组均开启。

| 指标 | Base | V3 |
|---|---:|---:|
| Judge 1 Precision | 0.333 | 1.0 |
| Judge 1 Recall | 0.333 | 0.333 |
| Judge 1 F1 | 0.333 | 0.5 |
| Judge 2 Precision | 0.333 | 1.0 |
| Judge 2 Recall | 0.333 | 0.333 |
| Judge 2 F1 | 0.333 | 0.5 |
| Judge 1 完全正确样本 | 1 | 2 |
| Judge 2 完全正确样本 | 1 | 2 |
| 双 Judge 一致样本 | 4/30 | 4/30 |
| 平均延迟 ms | 66089.53 | 76719.44 |
| 主链路模型调用 | 4 | 21 |
| 工具调用 | 0 | 12 |

> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个有标准答案的 Judge 裁决。
> execution_completed=false