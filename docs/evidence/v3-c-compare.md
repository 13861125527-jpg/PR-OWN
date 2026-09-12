# V3-C 会话压缩对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v3c_compare` 生成；原始 JSON：`docs/evidence/v3-c-compare.json`。
> dataset=`v3c_compact` samples=8 real_api=False
> 压缩由程序生成；OQ-11 live 后置。脱敏：不含 API Key / 源码原文。

## 压缩 / 证据索引 / 隔离

passed=8/8

| id | kind | ok | status |
|----|------|----|--------|
| compact-keeps-min-l3 | compact | True | kept |
| compact-keeps-evidence-ids | compact | True | completed |
| compact-not-a-round | compact | True | calls=3 |
| compact-native-pairing | compact | True | paired |
| compact-recompact-keeps-old-facts | compact | True | merged |
| compact-json-repair-keeps-repair-instruction | compact | True | completed |
| overflow-after-compact | compact | True | partial |
| default-agent-off | isolate | True | ok |

脚本化 Fake；压缩为程序生成。agent.enabled 默认 false。

全部通过：True
