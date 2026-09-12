# V3-B Agent Loop 对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v3b_compare` 生成；原始 JSON：`docs/evidence/v3-b-compare.json`。
> dataset=`v3b_loop` samples=14 real_api=False
> Agent 为 experimental；OQ-11 live 后置。脱敏：不含 API Key / 源码原文。

## 轨迹 / 控制工具 / 隔离 / 预算

passed=14/14

| id | kind | ok | status |
|----|------|----|--------|
| happy-read-submit-finish | trajectory | True | completed |
| unknown-tool-attempt | trajectory | True | completed |
| repeat-loop | trajectory | True | partial |
| idle-no-progress | trajectory | True | partial |
| grace-no-new-tools | trajectory | True | completed |
| stub-invoke-not-bound | control | True | error |
| default-review-isolation | isolate | True | ok |
| agentic-disabled-reject | isolate | True | rejected |
| early-grace-cap | trajectory | True | partial |
| json-repair-counts-round | trajectory | True | completed |
| unique-same-name-tool-ids | trajectory | True | completed |
| settle-idempotent | control | True | idempotent |
| waiting-tool-cancel | control | True | cancelled |
| shared-pool-concurrent | control | True | one_winner |

脚本化 Fake 轨迹；无真实模型。agent.enabled 默认 false。experimental。

全部通过：True
