# V2-E 反馈记忆对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v2e_compare` 生成；原始 JSON：`docs/evidence/v2-e-compare.json`。
> dataset=`reposage/evals/datasets/v2e_feedback.yaml` samples=4 real_api=not_run
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 反馈抑制 / 撤销 / 移动 / 隔离

passed=4/4 global_reject=True

| id | kind | ok | accepted | suppressed |
|----|------|----|----------|------------|
| suppress-false-positive | suppress | True | 0 | 1 |
| revoke-restores | restore | True | 1 | 0 |
| code-move-still-matches | move | True | 0 | 1 |
| feedback-disabled-ignores-mark | isolated | True | 1 | 0 |

mark 抑制 / 撤销恢复 / 代码移动 / 开关隔离；口径是程序状态，不是模型质量。

全部通过：True

真实 API 对照：未运行。
