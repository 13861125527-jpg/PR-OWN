# RepoSage V2 宽门控真实模型复测

> `deepseek-v4-pro`；MultiRole + L3；11 题；correctness=added_lines；Pipeline 阈值 0.0。

## 结果

| 指标 | 宽门控 V2 | 相对原 V2 同题 |
|---|---:|---:|
| Precision (macro) | 0.227 | 0.227 |
| Recall (macro) | 0.273 | 0.273 |
| F1 (macro) | 0.242 | 0.242 |
| 位置准确率（正样本 macro） | 0.273 | 0.273 |
| 负样本噪声 | 0 | 0 |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 0.25 |
| path + exact line（忽略 category） | 0.75 |
| path + ±5 lines（忽略 category） | 0.75 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 11/11 |
| 逻辑模型调用 | 22 |
| Provider 请求 | 22 |
| 输入 Token | 17693 |
| 输出 Token | 27320 |
| 平均端到端延迟 | 36409.77 ms |
| 中位延迟 | 31494.6 ms |
| L3 命中样本 | 0/11 |

> execution_completed=true