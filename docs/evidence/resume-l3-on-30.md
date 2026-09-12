# RepoSage V2 宽门控真实模型复测

> `deepseek-v4-pro`；MultiRole + L3；30 题；correctness=added_lines；Pipeline 阈值 0.0。

## 结果

| 指标 | 宽门控 V2 | 相对原 V2 同题 |
|---|---:|---:|
| Precision (macro) | 0.394 | n/a |
| Recall (macro) | 0.833 | n/a |
| F1 (macro) | 0.494 | n/a |
| 位置准确率（正样本 macro） | 0.792 | n/a |
| 负样本噪声 | 6 | n/a |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 0.792 |
| path + exact line（忽略 category） | 0.917 |
| path + ±5 lines（忽略 category） | 0.917 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 30/30 |
| 逻辑模型调用 | 60 |
| Provider 请求 | 66 |
| 输入 Token | 59094 |
| 输出 Token | 108712 |
| 平均端到端延迟 | 47753.99 ms |
| 中位延迟 | 38952.85 ms |
| L3 命中样本 | 0/30 |

> execution_completed=false