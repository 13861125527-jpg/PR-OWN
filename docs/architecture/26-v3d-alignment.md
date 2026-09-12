# V3-D 设计对齐（跨文件对照评测）

> 状态：**IMPLEMENTING**（按 §12 DP 落地；用户已授权开始编码）
> 前置：V3-C **ACCEPTED**（`docs/evidence/v3-c-implementation-review-round2.md`）
> 用户授权：V3-C 验收后进入 V3-D（本对齐稿）
> 契约来源：`01` FR-25/FR-26、`08` §9 方案 A/B、`11` §5–§8、`12` V3-d / ADR-005、`14` OQ-11、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §8 V3-D、V3-C `25` DP-10/DP-11
> DoD 摘要：**同一批跨文件样本上跑 V2 固定管道 vs V3 Agent；报告质量/成本/延迟三表 + 证据可追溯/工具有效率/重复率/失败率；native vs action_json 同集对照；默认 `agent.enabled=false`；不升 `user_version`；不调用真实模型（OQ-11 live 仍后置）；不因 Fake 差值改产品默认；V1–V3-C 回归通过**

---

## 0. 一句话与边界

V3-D 不交付新的审查能力，只交付 **对照实验与报告**：在同一批跨文件合成样本上，比较当前默认 V2 管道与显式打开的 V3 Agent（含 V3-A/B/C 工具、loop、压缩）。报告必须同时给出质量、成本、延迟；并计算 groundedness、工具有效率、重复调用率、工具次数、失败率。方案 A（native）与方案 B（action_json）在同一脚本化轨迹上对照。

**本里程碑禁止**：默认打开 `agent.enabled`、把 Fake 差值写成「真实模型质量收益」、恢复 OQ-11 live（仍 `DEFERRED_BY_USER`）、MCP、写工具、升 SQLite `user_version`、改 V2-C/D/E 语义或 `fingerprint` / `execute` 签名、复制审查主流程、在 loop 内推进 Finding 正式状态、开始 v1.0.0 Action。

若跨文件收益在本切片（Fake）或后续 live 中不显著，产品结论是 **保持 Agent experimental**，可降级叙述为「增强 V2 + 工具验证」，而不是把 Agent 设为默认。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V3-D 用法 | 缺口 |
|---|---|---|---|
| 评测模型 | `EvalDataset` / `EvalSample.kind=cross_file` / `compute_metrics` | 跨文件样本 + P/R/F1/位置 | 无跨文件 yaml；无 groundedness / 工具有效率 |
| 对照入口 | `v2a_compare` 三表（质量/成本/延迟） | 表结构与脱敏口径照抄 | 只比 V1 vs V2-A；不跑 Agent |
| 真实对照 | `evals/real_compare.py` | **不接**；live 仍后置 | OQ-11 `DEFERRED_BY_USER` |
| V2 管道 | `ReviewService` 默认 `single_pass`，`symbol_retrieval=true` | V2 臂 | 默认路径不会读工具 |
| V3 管道 | `strategy=agentic` + `agent.enabled=true` → `AgenticReviewer` | V3 臂，仅对照进程内打开 | 默认 Settings 必须仍 false |
| 协议 | `tool_protocol=native\|action_json` | 同集 A/B | Fake 下 A/B 行为接近，报告必须写明 |
| 证据 | `Finding.evidence[].tool_call_id`；`tool_calls` 表 | groundedness | 无现成聚合函数 |
| Agent 计数 | `successful_tools` / `tool_attempts` / `repeat_count` / `stop_reason` | 有效率与重复率 | 对照脚本未采集 |
| 存储 | `user_version=5` | 不升版本；对照可用 `:memory:` | 无新表 |
| 既有对照 | v2a–v2e、v3a/b/c | 钉死默认 `agent.enabled=false` | 无 V3 vs V2 跨文件表 |

**明确不复用**：把 `real_compare` 改成默认 CI 入口、用 LLM summarizer 写评测结论、为对照升 schema。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-26；FR-25 的评测切片；`12` V3-d；`11` §7）

1. **同集对照**：同一 `EvalSample`（同一 `base_files`/`head_files`）分别走 V2 臂与 V3 臂。
2. **三张主表**：质量（P/R/F1/位置准确率/负样本噪声）、成本（Fake 记账：调用数/token/费用估算）、延迟（墙钟）。缺一不可。
3. **Agent 运行表**：工具调用数、成功数、attempt、有效率、重复率、任务失败/PARTIAL 率、groundedness。
4. **协议 A/B**：同一脚本化轨迹用 `native` 与 `action_json` 各跑一遍；报告 parse/完成情况，**不**在 Fake 上宣称谁更聪明。
5. **跨文件样本**：缺陷证据不在变更文件正文里（或 L3 按符号检索也拿不到），V3 需 `read_file` / `search_code` / `find_references` 才能命中。
6. **可追溯**：V3 命中样本的 accepted Finding 能回到 `evidence.tool_call_id`（及可选 DB 行）。
7. **默认路径不变**：对照结束，仓库默认仍 `agent.enabled=false`、`review.strategy=single_pass`。
8. **可测**：全部 Fake；CI 不读 `MODEL_API_KEY`。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| 真实模型 V2 vs V3 质量结论 | live / OQ-11 | 用户已后置；Fake 不能代替 |
| 默认打开 Agent | — | Round 2 起一直冻结 |
| 因 Fake Recall 更高而改默认 strategy | — | 交接：收益不显著不强留；live 前证据不足 |
| MCP、写工具、ruff-as-tool | — | 与 V3-A/B/C 相同 |
| 升 `user_version` | — | 无新持久化 |
| 改 fingerprint / Pipeline / `execute` 签名 | — | 回归铁律 |
| 把 V3-C 压缩算法再改一遍 | — | 本切片只消费它 |

### 2.3 与架构原文的已知差异（必须显式）

| 架构原文 | V3-D 裁决 | 处理 |
|----------|-----------|------|
| `11` §7「Recall/groundedness 提升有报告背书」 | 报告必须有表；**Fake 提升只证明实验能测出差值** | **DP-1** |
| `11` §7 工具有效率 ≥ 0.7、重复率 < 0.3 | 作为 **计量口径 + 脚本化门槛样本**，不是真实模型 SLA | **DP-4** |
| `08` §9 / OQ-11 同集 A/B 选方案 | Fake A/B 只证明双协议可跑；**不关闭 OQ-11** | **DP-5** |
| `12`「若方案 B JSON 不足则 V3 暂缓」 | V3-A/B/C 已落地且默认关闭；本切片不回滚 | 产品默认已是「V2 + 可选 Agent」 |
| 交接「收益不显著可降级为增强 V2 + 工具验证」 | 本切片结论槽固定 `keep_agent_experimental` | **DP-6** |

---

## 3. 对照臂定义

### 3.1 V2 臂（固定管道）

进程内 `Settings`：

| 项 | 值 |
|----|----|
| `review.strategy` | `single_pass`（当前仓库默认） |
| `agent.enabled` | `false` |
| `context.symbol_retrieval` | `true`（当前默认，不关 L3） |
| `review.static.enabled` | `false` |
| `review.judge.enabled` | `false` |
| `review.feedback.enabled` | `true`，但对照用空库，无历史反馈 |

LLM：`FakeLLMProvider`。为避免「脚本直接把答案塞进 V2」，V2 Fake **仅当 prompt 中出现样本约定的证据锚点**（helper 文件里的独特标识）才产出对应 Finding；否则空列表。这样 L3 真正检索到 helper 时 V2 可以命中；检索不到则 miss。

### 3.2 V3 臂（Agent）

同一进程、**另一份** Settings：

| 项 | 值 |
|----|----|
| `review.strategy` | `agentic` |
| `agent.enabled` | `true`（仅对照） |
| `agent.tool_protocol` | 先 `native`，A/B 表再跑 `action_json` |
| 其余预算 | 沿用 `AgentConfig` 默认（含 V3-C compact 字段） |

LLM：`FakeLLMProvider(tool_script=…)`，按样本脚本：读 helper / 搜索 → `submit_finding(evidence_tool_call_ids=…)` → `finish_review`。

Workspace：`MemoryToolSnapshot(head_files)`，与 V2 同一 head 快照。

### 3.3 公平性

- 同一 `GlobalBudget` 初值量级（V3 仍走 reserved finalize，不放宽硬顶）。
- 同一 `FindingPipeline` 与 `min_confidence`。
- 不把 V3 的 `submit_finding` 候选绕过 Pipeline。
- 对照不得修改全局默认配置文件。

---

## 4. 数据集

路径：`reposage/evals/datasets/v3d_cross_file.yaml`。

建议 6–8 条合成 Python 样本（可少于 V1 的 20；本切片主题是跨文件，不是再刷 V1 门槛）：

| 类 | 作用 |
|----|------|
| `l3-hit` | diff 里的符号名与 helper 定义一致，L3 大概率把 helper 拉进 V2 prompt → V2 可命中 |
| `l3-miss` | 间接/动态/字符串拼路径，L3 按符号拿不到 helper → 期望 V2 miss、V3 工具命中 |
| `negative` | 跨文件改动但无缺陷 → 两侧噪声 |
| `grounded` | V3 必须带 `tool_call_id`；去掉 id 的负例只作计量，不进「V3 优于 V2」叙事 |
| `spam-tools`（可选） | 故意重复/无效工具，用于钉死有效率分母 |

样本字段沿用 `EvalSample`。`notes` 标明 `l3_expected: hit|miss`。文件内容不得含真实密钥；可用明显假口令/假 token 作锚点。

**不**把 `v1_demo.yaml` 全量再跑一遍当 V3-D 主表（那是单文件 V1 集）。可选附录：在 v1_demo 上 V3 不崩溃、默认关闭时仍零 `tool_calls`。

---

## 5. 指标口径

沿用 `compute_metrics`（一对一、category 必须一致）。新增只读聚合，不改 Pipeline。

| 指标 | 定义 | 分母 |
|------|------|------|
| Precision / Recall / F1 / 位置准确率 / 负样本噪声 | 现有 `Metrics` | 每样本再 macro |
| **groundedness** | accepted Finding 中，至少一条 `evidence.tool_call_id` 能在本 run `tool_calls`（或会话索引）对上，**或** canonical 落在本次 diff 行 | accepted 条数；0 条则记 `n/a` 不记 1.0 |
| **tool_effectiveness** | `successful_tools / max(tool_attempts, 1)`；控制工具 `submit_finding`/`finish_review` **不计入** attempt | V3 臂 |
| **repeat_rate** | `repeat` 错误观察次数 / `max(tool_attempts, 1)` | V3 臂 |
| **tool_calls** | 只读工具成功次数（及 attempts） | V3 臂；V2 必须为 0 |
| **failure_rate** | AgentTask `PARTIAL`+`FAILED` / AgentTask 总数 | V3 臂 |
| **cost** | Fake `ModelUsage` 累计 input/output/calls；非账单 | 两臂 |
| **latency** | 该臂跑完数据集的墙钟 | 两臂 |

`11` §7 的 0.7 / 0.3：在 **高效脚本样本** 上有效率 ≥ 0.7 且重复率 < 0.3；在 **spam 样本** 上有效率必须能低于 0.7（证明不是 tautology）。**不**把「全数据集 macro 有效率 ≥ 0.7」写成真实模型验收。

---

## 6. 报告

`python -m reposage.evals.v3d_compare` 写：

- `docs/evidence/v3-d-compare.json`
- `docs/evidence/v3-d-compare.md`

Markdown 至少：

1. 质量表：V2 vs V3（macro）
2. 成本表
3. 延迟表
4. Agent 运行表（有效率/重复/失败/groundedness/工具次数）
5. 协议 A/B 表（native vs action_json：完成数、calls、是否全部 `all_passed` 脚本）
6. 逐样本：id、`l3_expected`、V2 hit、V3 hit、V3 tool_call_ids
7. 固定声明：`real_api=false`；不宣称线上质量；`recommendation=keep_agent_experimental`
8. 脱敏：无 API Key、无完整源码原文

主表 **就是** P/R/F1（与 V3-C 不同）。

---

## 7. 协议 A/B（`08` §9）

| | 方案 A native | 方案 B action_json |
|--|---------------|-------------------|
| Fake | 同一 `tool_script` 语义 | 同一脚本，经 action JSON 解析路径 |
| 断言 | 跨文件命中与 groundedness 不因协议丢失 | 同上；可含 1 条 parse_error→repair 仍完成 |
| live | 仍 OQ-11，本切片不跑 | 同左 |

Fake 下两列数字接近是预期。报告写「协议可切换；选型等 live」。默认 `tool_protocol` 配置值仍为 `native`（experimental），本切片不改默认。

---

## 8. 存储 / 安全 / 兼容

- `user_version` 保持 **5**。
- 对照用内存 SQLite；不把 compact blob 落库。
- 日志沿用脱敏；评测 JSON 不写 prompt 全文、不写 Key。
- V2-A–E、V3-A–C 对照继续钉死默认 `agent.enabled=false`。
- 不改 `ReviewStrategy.execute` 签名。

---

## 9. 评测与测试

### 9.1 Fake 必须覆盖

- 至少一条 `l3-miss`：V2 recall 0、V3 recall 1，且 Finding `evidence.tool_call_id` 非空。
- 至少一条 `l3-hit`：V2 与 V3 均可命中（L3 生效时）。
- 负样本：V3 不因「读了 helper」就乱报（脚本不 submit）。
- 默认 `ReviewService` 仍零 `tool_calls`。
- `v3c_compare` 8/8、`v3b_compare` 14/14 仍绿。
- native 与 action_json 两臂都跑完同一跨文件正例。
- 高效脚本有效率 ≥ 0.7；spam 脚本有效率 < 0.7。

### 9.2 对照 `v3d_compare`

数据集 `reposage/evals/datasets/v3d_cross_file.yaml`。CI 断言：报告文件可生成、三主表存在、`real_api is False`、`recommendation == keep_agent_experimental`、默认 agent 关闭。**不**断言「V3 F1 全局大于 V2」作为产品真理（那是脚本构造）。

---

## 10. 分步实施任务卡

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查与修订 | 本文冻结 | — |
| T1 | 跨文件 yaml + 证据锚点约定 | 样本可被 FakeGit 加载 | T0 |
| T2 | groundedness / 有效率 / 重复率纯函数 | 单测钉死分母 | T0 |
| T3 | `v3d_compare`：V2 臂 + V3 臂 + 三表 | md/json | T1, T2 |
| T4 | 协议 A/B 表 + Agent 运行表 | 8 项左右程序断言 | T3 |
| T5 | `tests/test_evals.py` 钉死 v3d；保留 A–E / V3-A/B/C | 默认 agent off | T4 |
| T6 | 全量 pytest / Ruff / mypy | CI | T5 |

目录（预计）：

```text
reposage/evals/v3d_compare.py
reposage/evals/datasets/v3d_cross_file.yaml
docs/evidence/v3-d-compare.md
docs/evidence/v3-d-compare.json
docs/evidence/v3-d-status.md
```

改动点应几乎只在 `evals/` 与 `docs/evidence/`；`ReviewService` / Agent loop **无功能改动**，除非对照发现统计缺口（只加只读聚合，不改状态机）。

---

## 11. DoD 与审查清单

### 11.1 里程碑 DoD

- [ ] 跨文件同集 V2 vs V3 报告含质量/成本/延迟三表
- [ ] groundedness、工具有效率、重复率、失败率有口径与数字
- [ ] native 与 action_json 同集 Fake 对照
- [ ] 默认 Agent 关闭；无 live API
- [ ] `user_version == 5`
- [ ] V3-C 8/8、V3-B 14/14 与全量回归绿
- [ ] 报告结论为 `keep_agent_experimental`，不改默认 strategy

### 11.2 实现期禁止项

- [ ] 不读 `MODEL_API_KEY` / 不发真实 HTTP
- [ ] 不默认 `agent.enabled=true`
- [ ] 不把 Fake 当真实质量宣传
- [ ] 不升 schema
- [ ] 不开始 v1.0.0 / MCP / 写工具

---

## 12. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | CI DoD 是脚本化 Fake 对照；禁止用 Fake 宣称真实模型收益 | 与 V2-A 三表同一纪律；OQ-11 仍后置 |
| **DP-2** | V2 臂 = 仓库默认管道（`single_pass` + L3 on + agent off），不关 L3 来「制造」V3 赢面 | 对照的是真实默认，不是残缺 V2 |
| **DP-3** | V3 臂仅在对照进程打开 `agent.enabled`；仓库默认仍 false | 冻结项 |
| **DP-4** | 0.7 / 0.3 钉在脚本化正/负样本上，不是 live SLA | 无真实工具轨迹前不能签 SLA |
| **DP-5** | A/B 只证明双协议可跑；不关闭 OQ-11，不改默认 `tool_protocol` | live 才能选方案 |
| **DP-6** | 本切片 `recommendation` 固定 `keep_agent_experimental` | 交接允许降级；live 前证据不足，不能 promote |
| **DP-7** | 不改 V2 语义、不改 `execute` 签名、不改 fingerprint、不升 `user_version` | 回归铁律 |
| **DP-8** | 主数据集是新建跨文件集，不是全量 `v1_demo` | V3-D 主题是跨文件 |
| **DP-9** | 统计缺口只加 eval 纯函数，不改 Agent 状态机 | V3-C 已验收 |
| **DP-10** | OQ-11 live 仍 `DEFERRED_BY_USER`；本切片不接 `real_compare` | 用户未授权真实 API |

若需推翻某条，只改本节与对应章节。

---

## 13. 审查时请确认

1. DP-1 / DP-6：本切片是否接受「只出 Fake 三表 + 永不 promote 默认 Agent」？若必须 live 质量数字才能算 V3-D 完成，则范围扩大，需另开授权。
2. DP-2：V2 臂是否坚持默认 `single_pass`（而不是 `multi_role`）？
3. 跨文件样本 6–8 条是否够；是否要求 ≥20。
4. 是否继续 **不** 跑真实 API。

审查通过后按任务卡实现。默认 `agent.enabled=false`。OQ-11 live 仍后置。
