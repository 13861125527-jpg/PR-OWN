# V3 真实模型 30 题：Base 对照

> 主模型 `deepseek-v4-pro`；双语义 Judge `deepseek-v4-flash`；产品证据 Judge 两组均开启。

| 指标 | Base | V3 |
|---|---:|---:|
| Judge 1 Precision | 0.9 | 0.667 |
| Judge 1 Recall | 0.818 | 0.364 |
| Judge 1 F1 | 0.857 | 0.471 |
| Judge 2 Precision | 0.9 | 0.667 |
| Judge 2 Recall | 0.818 | 0.364 |
| Judge 2 F1 | 0.857 | 0.471 |
| Judge 1 完全正确样本 | 12 | 6 |
| Judge 2 完全正确样本 | 12 | 6 |
| 双 Judge 一致样本 | 15/30 | 15/30 |
| 平均延迟 ms | 79466.43 | 70664.68 |
| 主链路模型调用 | 15 | 78 |
| 工具调用 | 0 | 48 |

> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个有标准答案的 Judge 裁决。
> execution_completed=false