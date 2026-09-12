# RepoSage V2 真实模型 40 题评测

> `deepseek-v4-pro`；MultiRole（general/security/correctness/performance）+ L3；40 题；Pipeline 阈值 0.0。

## 结果

| 指标 | V2 | 相对 V1 |
|---|---:|---:|
| Precision (macro) | 0.0 | -0.679 |
| Recall (macro) | 0.0 | -0.775 |
| F1 (macro) | 0.0 | -0.687 |
| 位置准确率（正样本 macro） | 0.0 | -0.7 |
| 负样本噪声 | 0 | -4.0 |

## 更可解释的缺陷级指标

| 指标 | V2 |
|---|---:|
| category + path + exact line | 1.0 |
| path + exact line（忽略 category） | 1.0 |
| path + ±5 lines（忽略 category） | 1.0 |

## 调用与延迟

| 项 | 数值 |
|---|---:|
| 成功样本 | 0/40 |
| 逻辑模型调用 | 0 |
| Provider 请求 | 0 |
| 输入 Token | 0 |
| 输出 Token | 0 |
| 平均端到端延迟 | 0 ms |
| 中位延迟 | 0 ms |
| L3 命中样本 | 0/0 |

> execution_completed=false