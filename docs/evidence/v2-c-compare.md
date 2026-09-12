# V2-C 转换/去重对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v2c_compare` 生成；原始 JSON：`docs/evidence/v2-c-compare.json`。
> dataset=`reposage/evals/datasets/v2c_static.yaml` samples=8 real_api=not_run
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 转换

passed=3/3

| id | ok | n_candidates |
|----|----|--------------|
| convert-b006 | True | 1 |
| reject-traversal | True | 0 |
| reject-old-line | True | 0 |

诊断 → Candidate 的路径/新增行过滤；不跑真实模型。

## 去重 / 融合

passed=5/5

| id | kind | ok | n_accepted | sources |
|----|------|----|------------|---------|
| dedup-same-rule | dedup | True | 1 | static_analyzer |
| distinct-rules | dedup | True | 2 | static_analyzer |
| fuse-llm-static | fuse | True | 1 | llm_general,static_analyzer |
| no-fuse-different-category | no_fuse | True | 2 | llm_general,static_analyzer |
| no-fuse-two-static-via-llm | no_fuse | True | 2 | llm_general,static_analyzer |

同码去重、异码并存、LLM+静态融合；fingerprint 用静态 rule_id。

## 成本

本对照不经 LLM；模型 calls 相对 V2-B 不变由单测/Service 开关保证。静态分析不占 GlobalBudget。

全部通过：True

真实 API 对照：未运行。
