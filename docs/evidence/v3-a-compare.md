# V3-A 工具沙箱对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v3a_compare` 生成；原始 JSON：`docs/evidence/v3-a-compare.json`。
> dataset=`v3a-sandbox` samples=10 real_api=False
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 越界 / 合法读 / 截断 / stub / 隔离

passed=10/10

| id | kind | ok | status | truncated |
|----|------|----|--------|-----------|
| escape-absolute | escape | True | invalid_args | False |
| escape-dotdot | escape | True | invalid_args | False |
| read-ok | read | True | ok | False |
| truncate-read | truncate | True | ok | True |
| search-literal | search | True | ok | False |
| search-regex-not-interpreted | search | True | ok | False |
| stub-submit | stub | True | error | False |
| stub-finish | stub | True | error | False |
| unknown-tool | schema | True | invalid_args | False |
| default-review-isolation | isolate | True | ok | False |

脚本化 Fake 快照；无真实模型。不贴源码原文。

全部通过：True

真实 API 对照：未运行。
