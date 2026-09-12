# RepoSage V2 宽门控真实模型复测

> `deepseek-v4-pro`；MultiRole + L3；9 题；correctness=added_lines；Pipeline 阈值 0.0。

## 结果

| 指标 | 宽门控 V2 | 相对原 V2 同题 |
|---|---:|---:|
| Precision (macro) | 0.667 | n/a |
| Recall (macro) | 1.0 | n/a |
| F1 (macro) | 0.667 | n/a |
| 位置准确率（正样本 macro） | 1.0 | n/a |
| 负样本噪声 | 3 | n/a |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 1.0 |
| path + exact line（忽略 category） | 1.0 |
| path + ±5 lines（忽略 category） | 1.0 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 9/9 |
| 逻辑模型调用 | 23 |
| Provider 请求 | 28 |
| 输入 Token | 26047 |
| 输出 Token | 48221 |
| 平均端到端延迟 | 74104.05 ms |
| 中位延迟 | 44018.24 ms |
| L3 命中样本 | 9/9 |

> execution_completed=false