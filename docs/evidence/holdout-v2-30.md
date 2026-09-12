# RepoSage V2 宽门控真实模型复测

> `deepseek-v4-pro`；MultiRole + L3；30 题；correctness=added_lines；Pipeline 阈值 0.0。

## 结果

| 指标 | 宽门控 V2 | 相对原 V2 同题 |
|---|---:|---:|
| Precision (macro) | 0.467 | n/a |
| Recall (macro) | 0.667 | n/a |
| F1 (macro) | 0.467 | n/a |
| 位置准确率（正样本 macro） | 0.583 | n/a |
| 负样本噪声 | 8 | n/a |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 0.583 |
| path + exact line（忽略 category） | 1.0 |
| path + ±5 lines（忽略 category） | 1.0 |
| V4 FLASH 语义 Precision（micro） | 0.562 |
| V4 FLASH 语义 Recall（micro） | 0.75 |
| V4 FLASH 语义 F1（micro） | 0.643 |
| 类别一致率（仅诊断） | 0.722 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 30/30 |
| 逻辑模型调用 | 60 |
| Provider 请求 | 64 |
| 输入 Token | 54436 |
| 输出 Token | 108040 |
| 平均端到端延迟 | 56704.36 ms |
| 中位延迟 | 54706.5 ms |
| L3 命中样本 | 17/30 |

> execution_completed=true