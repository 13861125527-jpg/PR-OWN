# RepoSage V2 宽门控真实模型复测

> `deepseek-v4-pro`；MultiRole + L3；6 题；correctness=added_lines；Pipeline 阈值 0.0。

## 结果

| 指标 | 宽门控 V2 | 相对原 V2 同题 |
|---|---:|---:|
| Precision (macro) | 1.0 | n/a |
| Recall (macro) | 1.0 | n/a |
| F1 (macro) | 1.0 | n/a |
| 位置准确率（正样本 macro） | 0.0 | n/a |
| 负样本噪声 | 0 | n/a |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 1.0 |
| path + exact line（忽略 category） | 1.0 |
| path + ±5 lines（忽略 category） | 1.0 |
| V4 FLASH 语义 Precision（micro） | 1.0 |
| V4 FLASH 语义 Recall（micro） | 1.0 |
| V4 FLASH 语义 F1（micro） | 1.0 |
| 类别一致率（仅诊断） | 0.0 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 6/6 |
| 逻辑模型调用 | 18 |
| Provider 请求 | 19 |
| 输入 Token | 17839 |
| 输出 Token | 33723 |
| 平均端到端延迟 | 70763.02 ms |
| 中位延迟 | 65979.0 ms |
| L3 命中样本 | 6/6 |

> execution_completed=true