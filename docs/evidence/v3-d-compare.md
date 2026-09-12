# V3-D 跨文件 V2 vs V3 对照（脚本化 Fake，非真实 API）

> 由 `python -m reposage.evals.v3d_compare` 生成；原始 JSON：`docs/evidence/v3-d-compare.json`。
> dataset=`reposage/evals/datasets/v3d_cross_file.yaml` samples=7 repeats=3 real_api=False
> recommendation=`keep_agent_experimental`
> 脱敏：不含 API Key / 源码原文 / 密钥字面量。

## 质量（Finding，macro）

| 指标 | V2 single_pass | V3 agentic |
|------|----------------|------------|
| Precision | 0.4 | 1.0 |
| Recall | 0.4 | 1.0 |
| F1 | 0.4 | 1.0 |
| 位置准确率 | 0.25 | 1.0 |
| 负样本噪声 | 0.0 | 0.0 |

脚本化 Fake；V2 仅当 L3 prompt 含锚点才吐 Finding。带 tool_call_id 的 V3 候选只有 TOOL_RESULT，Pipeline 不验证该证据并打 0.8 折；脚本用 confidence=1.0（折后 0.8）才能过默认 min_confidence=0.75。若用与 V2 相同的 0.9，折后 0.72 会被抑制。xf-spam-tools / xf-ungrounded 不进本表。不代表真实模型质量。

## 成本（Fake 记账）

| 指标 | V2 single_pass | V3 agentic |
|------|----------------|------------|
| 模型调用数 | 7 | 23 |
| input tokens | 700 | 2300 |
| output tokens | 350 | 1150 |
| total tokens | 1050 | 3450 |
| 只读工具次数 | 0 | 7 |
| cost_usd | 0.007 | 0.023 |

Fake ModelUsage 累计（含 cost_usd），非真实 API 账单。

## 延迟（全数据集墙钟）

| 指标 | V2 single_pass | V3 agentic |
|------|----------------|------------|
| total_ms | 44.23 | 68.5 |
| p50_ms | 42.15 | 68.47 |
| p95_ms | 47.94 | 70.8 |

全数据集墙钟，repeats=3；质量取第 1 次。Fake 延迟不代表真实模型。

## Agent 运行

| 指标 | V3 |
|------|----|
| 只读工具次数 | 7 |
| successful_tools | 7 |
| tool_attempts | 10 |
| 有效率 | 0.7 |
| 重复率 | 0.3 |
| 失败/PARTIAL 率 | 0.000 |
| groundedness | 1.000 |
| tool_groundedness | 0.833 |
| needs_evidence（accepted） | 5 |
| V2 工具次数 | 0 |

控制工具不计 attempt。本表是数据集合计，不是 0.7/0.3 门槛；门槛只钉 xf-l3-miss / xf-spam-tools。groundedness 含 diff 行（Pipeline 已定位则常为 1.0）；tool_groundedness 只认 evidence.tool_call_id。V3 带 TOOL_RESULT 的 accepted 常 needs_evidence=True（Pipeline 不验证该证据）。

## 协议 A/B（native vs action_json）

native_completed=7/7 action_json_completed=7/7 all_passed=True hits_preserved=True groundedness_preserved=True

| id | native 完成 | action 完成 | native hit | action hit | native calls | action calls | action tool_call_ids | action repair |
|----|-------------|-------------|------------|------------|--------------|--------------|----------------------|---------------|
| xf-l3-hit | True | True | 1 | 1 | 3 | 3 | aj-read_file-63dbbce9d038 | False |
| xf-l3-miss | True | True | 1 | 1 | 3 | 4 | aj-read_file-bde3a5f6e50e | True |
| xf-l3-miss-search | True | True | 1 | 1 | 3 | 3 | aj-search_code-eca59cd3fbdc | False |
| xf-negative | True | True | 0 | 0 | 2 | 2 |  | False |
| xf-grounded | True | True | 1 | 1 | 3 | 3 | aj-find_references-dcaeeafc0e52 | False |
| xf-spam-tools | True | True | 1 | 1 | 6 | 6 | aj-read_file-0d518ff1ad68 | False |
| xf-ungrounded | True | True | 1 | 1 | 3 | 3 |  | False |

action_json 臂经真实 Action JSON 解析器，命中样本 tool_call_id 为 aj-*；xf-l3-miss 另含 1 条 parse_error→repair。Fake 下两列接近是预期。协议可切换；选型等 live。不关闭 OQ-11。

## 逐样本

| id | l3_expected | V2 hit | V3 hit | V3 tool_call_ids | needs_evidence | ids_in_db | path_ok | anchor |
|----|-------------|--------|--------|------------------|----------------|-----------|---------|--------|
| xf-l3-hit | hit | 1 | 1 | xf-l3-hit-read | 1 | True | True | True |
| xf-l3-miss | miss | 0 | 1 | xf-l3-miss-read | 1 | True | True | True |
| xf-l3-miss-search | miss | 0 | 1 | xf-l3-miss-search-search | 1 | True | True | True |
| xf-negative | n/a | 0 | 0 |  | 0 | True | False | False |
| xf-grounded | miss | 0 | 1 | xf-grounded-refs | 1 | True | True | True |
| xf-spam-tools | miss | 0 | 1 | xf-spam-tools-r0 | 1 | True | True | True |
| xf-ungrounded | miss | 0 | 1 |  | 0 | True | False | False |

## 脚本门槛（非 live SLA）

xf-l3-miss 有效率=1.0 重复率=0.0（门槛 ≥0.7 / <0.3）
xf-spam-tools 有效率=0.25（须 <0.7）
xf-grounded tool_groundedness=1.0 tool_call_ids=xf-grounded-refs
xf-ungrounded tool_call_ids 空、tool_groundedness=0.0（不进质量主表）
xf-spam-tools 有效率分母样本，不进质量主表

## 默认路径

V2 臂 = Settings() 默认（v2_is_default_settings=True）。
agent.enabled=False strategy=single_pass tool_protocol=native default_run_tool_calls=0
V3 臂只覆盖 strategy=agentic + agent.enabled + tool_protocol；file_tasks=3（v3_file_tasks_is_default=True）。

## 附录：v1_demo 默认关闭

sample=s01-eval agent.enabled=False strategy=single_pass tool_calls=0

real_api=False recommendation=`keep_agent_experimental` user_version=5 dataset_status=ok

真实 API 对照：未运行。OQ-11 live 仍后置。
