# V2-B L3-off vs L3-on 对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v2b_compare` 生成；原始 JSON：`docs/evidence/v2-b-compare.json`。
> dataset=`reposage/evals/datasets/v2b_compare.yaml` samples=4 repeats=3 real_api=not_run
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 检索命中（程序指标，主验收）

dataset=`reposage/evals/datasets/v2b_l3.yaml` hit_rate=1.0 false_rate=0.0 TP/FN/FP=9/0/0 expected=9 forbidden=6

按 expected/forbidden symbol 计数，不是按样本。

## 质量（Finding，macro）

| 指标 | L3-off | L3-on |
|------|--------|-------|
| Precision | 0.75 | 1.0 |
| Recall | 0.75 | 1.0 |
| F1 | 0.75 | 1.0 |
| 位置准确率 | 0.75 | 1.0 |

脚本化 Fake 把候选放在 L3-off/on；v2b-consume 仅当消息含 helper 定义才吐 Finding，证明管道吃到 L3，不代表真实模型增益。

## 成本（Fake 记账）

| 指标 | L3-off | L3-on |
|------|--------|-------|
| 模型调用数 | 4 | 4 |
| input tokens | 645 | 808 |
| output tokens | 200 | 200 |
| L3 chunks | 0 | 4 |
| 预算拒绝次数 | 0 | 0 |

input tokens 来自 ReviewUnit 消息的 estimate_tokens，不是 Fake 固定 100。

## 延迟（全数据集墙钟）

| 指标 | L3-off | L3-on |
|------|--------|-------|
| total_ms（均值） | 0.6 | 0.58 |
| p50_ms | 0.58 | 0.55 |
| p95_ms | 0.73 | 0.62 |

全数据集墙钟，repeats=3；Fake 延迟不代表真实模型。

真实 API 对照：未运行。
