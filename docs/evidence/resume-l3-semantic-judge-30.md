# RepoSage V2 宽门控真实模型复测

> `deepseek-v4-pro`；MultiRole + L3；30 题；correctness=added_lines；Pipeline 阈值 0.0。

## 结果

| 指标 | 宽门控 V2 | 相对原 V2 同题 |
|---|---:|---:|
| Precision (macro) | 0.711 | 0.317 |
| Recall (macro) | 0.8 | -0.033 |
| F1 (macro) | 0.717 | 0.223 |
| 位置准确率（正样本 macro） | 0.75 | -0.042 |
| 负样本噪声 | 3 | -3 |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 0.75 |
| path + exact line（忽略 category） | 0.792 |
| path + ±5 lines（忽略 category） | 0.792 |
| V4 FLASH 语义 Precision（micro） | 0.826 |
| V4 FLASH 语义 Recall（micro） | 0.792 |
| V4 FLASH 语义 F1（micro） | 0.809 |
| 类别一致率（仅诊断） | 0.947 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 30/30 |
| 逻辑模型调用 | 82 |
| Provider 请求 | 91 |
| 输入 Token | 84870 |
| 输出 Token | 128897 |
| 平均端到端延迟 | 82523.0 ms |
| 中位延迟 | 54138.07 ms |
| L3 命中样本 | 30/30 |

> execution_completed=false