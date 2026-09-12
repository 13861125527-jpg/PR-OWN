# V2-D 去重 / Judge 对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v2d_compare` 生成；原始 JSON：`docs/evidence/v2-d-compare.json`。
> dataset=`reposage/evals/datasets/v2d_dedup.yaml` samples=5 real_api=not_run
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 确定性去重

passed=2/2

| id | kind | ok | n_raw | n_merged | survival | collapse |
|----|------|----|-------|----------|----------|----------|
| collapse-five-duplicates | dedup | True | 5 | 1 | 0.2 | 0.8 |
| no-merge-distinct-triggers | no_merge | True | 2 | 2 | 1.0 | 0.0 |

重复候选压缩与不应合并的对；口径是 duplicate_survival_rate，不是真实重复占比。

## Judge

passed=3/3

| id | ok | n_accepted | keep | downrank |
|----|----|------------|------|----------|
| judge-downrank | True | 0 | 0 | 1 |
| judge-keep | True | 1 | 1 | 0 |
| judge-fail-open | True | 1 | 1 | 0 |

Fake 裁决 keep/downrank/fail-open；不宣称真实模型质量。

## 成本

本对照 Fake Adjudicator 不经 LLM complete；真实 Judge 成本见 real_compare（非本里程碑 DoD）。

全部通过：True

真实 API 对照：未运行。
