# V3 真实模型 30 题：Base 对照

> 主模型 `deepseek-v4-pro`；双语义 Judge `deepseek-v4-flash`；产品证据 Judge 两组均开启。

| 指标 | Base | V3 |
|---|---:|---:|
| Judge 1 Precision | 0.0 | 1.0 |
| Judge 1 Recall | 0.0 | 1.0 |
| Judge 1 F1 | 0.0 | 1.0 |
| Judge 2 Precision | 0.0 | 1.0 |
| Judge 2 Recall | 0.0 | 1.0 |
| Judge 2 F1 | 0.0 | 1.0 |
| Judge 1 完全正确样本 | 1 | 3 |
| Judge 2 完全正确样本 | 1 | 3 |
| 双 Judge 一致样本 | 3/30 | 3/30 |
| 平均延迟 ms | 130410.02 | 69506.11 |
| 中位延迟 ms | 117529.89 | 66636.12 |
| 精确行定位召回 | 0.0 | 1.0 |
| ±5 行定位召回 | 0.0 | 1.0 |
| 任务正常完成样本 | 1/30 | 3/30 |
| 主链路模型调用 | 3 | 12 |
| 工具调用 | 0 | 6 |
| 工具有效率 | n/a | 0.667 |
| 重复调用率 | n/a | 0.0 |

> V3-Base: Precision +1.0，Recall +1.0，F1 +1.0。

> Pipeline 只执行结构、位置、证据和去重校验；最终语义由两个有标准答案的 Judge 裁决。
> execution_completed=false