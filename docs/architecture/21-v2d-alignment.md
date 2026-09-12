# V2-D 设计对齐（聚类/去重冻结 + Judge keep/downrank）

> 状态：V2-D **ACCEPTED**（Round 1：`docs/evidence/v2-d-implementation-review-round1.md`；对照见 `docs/evidence/v2-d-status.md`）
> 前置：V2-A **ACCEPTED**；V2-B **ACCEPTED**；V2-C **ACCEPTED**（Round 2：`docs/evidence/v2-c-implementation-review-round2.md`）
> 契约来源：`01` FR-18/FR-19、`03` §4–§6 Pipeline 所有权、`04` §3 Judge 在 Pipeline、`05` §3–§4 状态机与不可写字段、`07` §5–§6、`09` §1/§4–§6 Judge prompt 与 routing、`10` §7–§9 预算/fail-soft、`11` §5–§8 重复率、`12` V2-d、`15` P0-1、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §7 V2-D
> DoD 摘要：**确定性 fingerprint / cross_run_match_key / 聚类先于指纹可测且冻结；Judge 仅 keep/downrank，不能改 canonical 事实、不能新增 Finding、不能宣布已验证；`needs_evidence` 为程序内部标记并在 V2 降为 body_only/suppressed；重复存活率/误合并/额外成本有对照；V1/V2-A/B/C 回归通过**

---

## 0. 一句话与边界

V2-D 分两层，且必须按这个顺序落地：

1. **确定性层**：把现有 `FindingPipeline` 的聚类、跨来源融合、fingerprint / `cross_run_match_key`（聚类后计算）写成可验收契约并冻结；补齐重复存活率口径。
2. **语义层**：在 merge 之后、置信度门槛之前，插入可插拔 Judge。Judge 对已合并 Finding 做一次轻量结构化裁决，输出只能是 `keep` / `downrank`。

`ReviewStrategy.execute` 主签名不变。Judge **不是角色**，不进 Role Registry，也不进 MultiRole。SinglePass 与 MultiRole 共用同一 Pipeline Judge。

**本里程碑禁止**：Agent / 工具循环、反馈记忆、watermark、改 Publishing、改 V2-C 融合规则（含 B2）、让 Judge 拆簇/并簇/加 Finding、默认打开 Judge、把 `needs_evidence` 做成模型输出枚举。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V2-D 用法 | 缺口 |
|---|---|---|---|
| 流水线 | `FindingPipeline.process`：schema → location → evidence → `_cluster` → `_fuse_cross_source` → `_merge_cluster` → 置信度门槛 | **唯一生命周期所有者**；Judge 插在 merge 与门槛之间 | `process` 同步、无 LLM、无 Judge、无 `needs_evidence` |
| 聚类 | 键 = path + category + 归一 trigger + 行重叠（容差 8） | 冻结；LLM–LLM 去重仍走这条 | 无语义聚类 |
| 跨来源融合 | V2-C：LLM↔静态、同 path/category/行重叠；静态 `rule_id` 并集 >1 拒绝 | **不改**（含 B2） | V2-C 残差（相邻行不同缺陷被融）**不能**靠 Judge 拆开 |
| 指纹 | `compute_fingerprint` / `compute_cross_run_match_key`；规则键 `rule_id or "model"` | 冻结公式；聚类后写入 | `cross_run` 的 symbol 锚仍是行号，V2-E 再强化代码移动 |
| 领域 | `FindingSourceKind.JUDGE`、`FindingVersion.actor` 已含 `judge`、`ModelUsage.role` 注释含 judge | 直接用 | Finding 无 `needs_evidence`；无 Judge 决策模型 |
| 策略 | `ReviewStrategy.execute` | 不改；仍只交 Candidate | 无 |
| 编排 | `ReviewService` PIPELINE 调 `pipeline.process` | 注入 Adjudicator + 同一 `GlobalBudget`；接收 `PipelineResult` 后统一落库 task/usage/coverage/stage | process 同步；REVIEW 结束后预算仍在，但 Pipeline 没用 |
| LLM | `LLMProvider.complete(..., schema=)` 已支持 Pydantic；`structured()` 专供 Finding 信封 | Judge 走 `complete` + 专用 schema，**不**复用 `structured()` | Fake 的 `complete` 需能按 schema 回放裁决 |
| Prompt | `prompts/roles/*.md` + `load_role_prompt` | 新增 `prompts/tasks/judge.md` | 无 task 加载器 |
| 配置 | `review.static`、`context.symbol_retrieval` | 新增 `review.judge`；`09` 已有 `llm.routing.judge` | Settings 无 judge 块 |
| 任务 | `ReviewTaskKind` 有 FILE/ROLE/STATIC/AGENT | 新增 `JUDGE_ADJUDICATE` | 无 |
| 存储 | findings / finding_versions / usages / tasks / coverages | versions 记 actor=judge；usage.role=judge；`needs_evidence` 列必加 | schema 仍为 v4 |
| 评测 | `v2a/b/c_compare`、`test_pipeline.py` | 钉死 `judge.enabled=false`；新增 v2d 重复存活率/裁决集 | “重复率”口径易混，必须用 §11 名称 |
| 发布 | Publisher 只发 accepted；body_only 进正文 | 不改规则 | downrank→suppressed 的 Finding 不会出现在评论里 |

**明确不复用为 V2-D 生产路径**：Agent `tool_loop`、反馈记忆、把 Judge 当第五个审查角色、用 Judge 改 V2-C 融合、GitHub blob API。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-18 / FR-19 / `07` §5–§6 / `11` §8 V2 重复率）

1. **确定性层可验收**：聚类先于指纹；同簇只留一条 Finding；fingerprint 含 head_sha+行锚；`cross_run_match_key` 不含 head_sha；V2-C 融合与 B2 行为不变。
2. **重复存活率可测**：口径见 §11；在标注的重复样本上报告，并对照 Judge 开关。
3. **Judge 可插拔**：默认关；打开后对 MERGED 且非 `needs_evidence` 的 Finding 做轻量裁决。
4. **输出受限**：只应用 `keep` / `downrank`；忽略一切事实字段、verified、新 Finding、`needs_evidence`。
5. **`needs_evidence` 内部化**：仅程序在「无已验证证据」时打标；V2 打开 Judge 时将这些候选降为 `body_only`（位置合法）或保持已有 `suppressed`，**不**送给 Judge，**不**等 V3 Agent。
6. **失败开放**：Judge 超时/预算不足/schema 失败 → 全部 keep，走原置信度门槛，warning + truncated，审查不整段失败。
7. **对照**：重复存活率、误合并率、误 downrank、额外 token/费用；V2-A/B/C compare 行为不变。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| Agent / 只读工具 / 补证重入流水线 | V3 | `needs_evidence` 在 V2 只降级，不触发补证 |
| 反馈记忆、watermark | V2-E | 发布 Saga 保持 V1-E |
| 改 `execute` 主签名 | — | 与 V2-A/B/C 同一铁律 |
| 让 Judge 合并、拆分、新增 Finding | — | `07` §6 禁止；拆融合是改确定性规则，不是 Judge |
| 改 V2-C `_clusters_fuseable` / B2 | — | 残差留下；Judge 不能把一条融找回两条 |
| 用 L3 符号重写 `cross_run_match_key` | V2-E | 交接「代码移动后仍可命中」属反馈匹配；改键会扰动发布幂等 |
| 把 Judge 注册成 role | — | ADR-004：Judge ≠ 审查角色 |
| 默认 `judge.enabled=true` | — | 改变发布集合；先评测 |
| 语义 embedding 聚类 | — | 超出本切片 |
| 改 Publishing / dry-run 默认 | — | downrank 后走现有状态机即可 |

### 2.3 与架构原文的已知差异（必须显式）

| 架构原文 | V2-D 裁决 | 处理 |
|----------|-----------|------|
| `07` §6「轻量裁决（单次调用）」 | 不是整个 Agent 会话；每 run 对 MERGED 集合按文件分块，每块一次 `complete` | DP-6 |
| `05`「merged → suppressed: downrank 出局」 | downrank → `SUPPRESSED`（actor=`judge`） | DP-7 |
| `11` V2 DoD 重复率 < 0.25 | **只**在 V2-D 标注重复集上作为门槛；single_pass 无重复时该比值接近 1，不能当全局不变量 | DP-12 |
| `09` YAML `context.symbol_retrieval: false` | 生产默认仍 True（V2-B）；Judge 与 L3 无关 | 对照脚本钉死 L3/static/judge 各开关 |
| `07` 流水线含 Judge | 默认关，与 static 同策略 | DP-4 |
| V2-C 对齐「残差交给 V2-D Judge」 | Judge **不能拆簇**；该残差本里程碑不修 | §4.3 |

---

## 3. 放置：Service / Pipeline / Strategy

```text
ReviewService.PIPELINE
  FindingPipeline.process(...)          # 仍是唯一状态推进者
    validate / locate / evidence
    cluster → fuse (V2-C，不改)
    merge + 写 fingerprint / cross_run / cluster_id
    needs_evidence 标记（程序；基于合并后的最终 evidence）
    [可选] FindingAdjudicator.adjudicate   # 可插拔；Pipeline 不 import OpenAI
    应用 keep/downrank
    置信度门槛 → accepted / suppressed
    排序
```

| 决策 | 结论 |
|------|------|
| Judge 在哪 | **Pipeline 阶段**，不在 Strategy |
| 谁调 LLM | `LlmAdjudicator`（注入）；Pipeline 只依赖 `FindingAdjudicator` 协议 |
| `execute` | **不改** |
| 静态分析 | 仍在 REVIEW 与 execute 并行；其 Candidate 已在 V2-C 进同一 Pipeline |
| 预算 | 与审查共用 `GlobalBudget`；Judge **占用** token/费用（与 static 相反） |

`process` 改为 `async` 并返回 `PipelineResult`：Judge 关闭时内部无 LLM await，行为与今天同步版相同。现有调用改为 `await`（Service、evals runner/baseline/real_compare、v2a/b/c_compare）。单测同样 await。**禁止**用 `asyncio.run` 包一层同步 API 给生产路径。

`FindingPipeline` 仍然不直接写 SQLite。Pipeline 只产生：

```text
PipelineResult
  findings: list[Finding]
  tasks: list[ReviewTask]                 # judge 分块任务；judge 关闭为空
  usages: list[ModelUsage]                # role="judge"；judge 关闭为空
  coverage_items: list[CoverageItem]      # judge skipped / truncated
  warnings: list[str]
  metrics: PipelineMetrics
```

```text
PipelineMetrics
  raw_candidates: int
  merged: int
  judge_enabled: bool
  judge_keep: int
  judge_downrank: int
  needs_evidence: int
  duplicate_survival_rate: float | None
  dedup_collapse_rate: float | None
```

`ReviewService` 负责把这些结果合入 `ReviewRun` 并调用 Storage 落库；避免把 Pipeline 变成第二个编排器。

---

## 4. 确定性层（先于 Judge，必须可测）

交接要求「先做确定性 fingerprint/cross_run key/cluster，再做语义层 Judge」。V1–V2-C 已经实现主干；V2-D **冻结并验收**，不另起算法。

### 4.1 聚类（冻结）

对 `LOCATION_VALID` 且走完 evidence 的 Finding：

- 同一簇：相同 `canonical_path`（缺则 claimed）、相同 `category`、归一化 `trigger_condition` 相同、行区间重叠或间距 ≤ `_CLUSTER_LINE_TOLERANCE`（8）。
- `body_only` / schema-`suppressed` **不聚类**（现状保持）。
- 两条 LLM 候选不因「同行同 category、trigger 不同」而合并。

### 4.2 指纹（冻结，聚类后计算）

在 `_merge_cluster` 写入，禁止在校验阶段预写后用模型值覆盖。

```text
fingerprint        = sha256(repo | head_sha | path | line_anchor | category | rule_key)
cross_run_match_key = sha256(repo | path | str(line_anchor) | category | rule_key)
rule_key           = 簇内唯一静态 rule_id，否则 "model"
```

断言：

- 同一 run 同一 fingerprint 只对应一条 Finding（DB `UNIQUE (run_id, fingerprint)` 已有）。
- `fingerprint != cross_run_match_key`（已有测）。
- 融合簇即使保留 LLM 文案，规则键仍用静态 `rule_id`（V2-C DP-9，不重开）。

### 4.3 明确不在确定性层改的东西

- V2-C `_fuse_cross_source` / `_clusters_fuseable`（含「静态 rule_id 并集 >1 拒绝」）。
- 用 trigger 语义或 Judge 去「反融合」。
- 用 L3 符号替换 `cross_run` 的行号锚。

V2-C 写下的残差（LLM 桥接相邻行不同缺陷）若仍出现，本里程碑用评测样本 **观测**，不修融合器、不用 Judge 拆条。

---

## 5. `needs_evidence`（内部标记，不是 Judge 输出）

### 5.1 何时打标（程序）

在 evidence 校验和 cluster merge 完成之后、Judge 之前确定最终值。原因：一个候选自身可能没有 verified 证据，但与静态结果/其它候选合并后获得 verified evidence；最终 `needs_evidence` 必须看合并后的 `Finding.evidence`，不能看单个 Candidate。

- `needs_evidence = True` 当且仅当合并后的 Finding **没有任何** `evidence[].verified is True`。
- 程序从 diff 补了真实新增行且 `verified=True` → **False**（与现逻辑一致）。
- 候选给了证据但全部重判失败、又未补到真实行 → **True**（现逻辑只把 confidence ×0.8 仍标 `EVIDENCE_VALID`）。

字段加在正式 `Finding` 上，默认 `False`。 **不**加入 LLM Finding schema，**不**加入 Judge schema。模型若夹带同名字段：忽略。

### 5.2 V2 行为（仅 `judge.enabled=true`）

| 条件 | 行为 |
|------|------|
| `needs_evidence` 且位置合法、已 merge | 不调用 Judge；`MERGED → BODY_ONLY`，actor=`program`，reason 含 `needs_evidence` |
| 已因未知路径等 `SUPPRESSED` | 保持 suppressed；仍可打标供观测 |
| `judge.enabled=false` | **只打标、不改门槛**（confidence ×0.8 后走现门槛）。默认 single_pass 发布集合与 V2-C 相同 |

V3 才允许 Agent 补证后重新进流水线。本里程碑无重入 API。

### 5.3 持久化

SQLite `findings` 增 `needs_evidence INTEGER NOT NULL DEFAULT 0`；`PRAGMA user_version` 4→5。旧库 `ALTER TABLE` 补列。评测与审计可读；不新增表。

---

## 6. Judge 契约

### 6.1 协议（Pipeline 依赖此，不依赖 Provider）

```python
class JudgeAction(StrEnum):
    KEEP = "keep"
    DOWNRANK = "downrank"

class JudgeDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 多字段丢掉
    finding_occurrence_id: str
    action: JudgeAction
    reason: str = ""

class JudgeBatchOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decisions: list[JudgeDecision]

class JudgeAdjudicationResult(BaseModel):
    output: JudgeBatchOutput
    tasks: list[ReviewTask] = Field(default_factory=list)
    usages: list[ModelUsage] = Field(default_factory=list)
    coverage_items: list[CoverageItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    partial: bool = False

class FindingAdjudicator(Protocol):
    async def adjudicate(
        self,
        *,
        run_id: str,
        findings: list[Finding],
        budget: GlobalBudget,
        model_semaphore: asyncio.Semaphore,
    ) -> JudgeAdjudicationResult: ...
```

`LlmAdjudicator` 与 `FakeAdjudicator` 都实现该协议。单测默认 Fake，不强制打真实模型。Fake 可以返回空 `tasks/usages`，但 LLM 路径必须返回 Judge task、usage、warning/coverage；否则 §7.3 和 §10 无法验收。

### 6.2 输入（程序组装，模型只看摘要）

每条送入 Judge 的 Finding 必须已是 MERGED、已有 fingerprint、`needs_evidence is False`。摘要字段：

- `finding_occurrence_id`（程序生成的 UUID，供回填）
- `title` / `category` / `severity` / `confidence`
- `canonical_path` / 行区间
- `trigger_condition` / `explanation` / `suggestion`（截断）
- 证据：`kind`、`location`、`verified`、`content` 截断；代码按现有 `wrap_untrusted` 边界包装
- `sources[].kind`（llm_general / llm_role / static_analyzer）

**禁止**放入：`verified` 以外的「请宣布已验证」指令、fingerprint 原文、head 整文件、ruff 原始 JSON dump、其它 Finding 的 canonical 供模型改写。

### 6.3 输出应用（程序）

对每个 MERGED Finding：

| 模型输出 | 程序 |
|----------|------|
| `keep` 或 **缺席该 id** | 不改 confidence/severity/文本/证据；可追加 `FindingSource(kind=JUDGE)`；然后走原 `min_confidence` 门槛 |
| `downrank` | **禁止**改 canonical 路径/行/evidence.verified/fingerprint；`MERGED → SUPPRESSED`，actor=`judge`，reason=模型 reason 经截断/脱敏 |
| 未知 id / 重复 id | 忽略多余；重复时第一次有效 |
| `needs_evidence` / `verified` / 新 findings / 改行号 | schema `extra=ignore` 或根本没有这些槽；即使 JSON 里有也 **不写入** Finding |
| 空 `decisions` / 解析失败 | 整批 keep（fail-open） |

keep **不是**强制 accepted：confidence 仍低于门槛则 program suppress。Judge **不能**上调 confidence 或 severity（否则等于伪造质量）。

downrank 的 Finding 仍落库，便于对照「被降掉的是什么」。Publisher 本来就不发 suppressed。

### 6.4 谁不进 Judge

- `judge.enabled=false`
- `needs_evidence=True`（已按 §5.2 降级）
- 尚未 MERGED 的终态（早期 body_only / schema suppressed）
- 超过 `max_findings` 的溢出项：按 severity×confidence 排序，只裁决前 N 条；溢出 **keep** + warning + coverage truncated（禁止把溢出当成 downrank）

### 6.5 分块

`07`「单次调用」= 不是多轮对话、不是 Agent。实现：

- 按 `canonical_path` 分块，每块 ≤ `max_findings`（默认 32）一次 `complete`。
- 单文件超过上限则该文件再按排序切开。
- 块与块独立 fail-open。

---

## 7. LLM 适配、Prompt、预算

### 7.1 不扩展 `structured()`

`LLMProvider.structured()` 的信封是 `findings[]`，让 Judge 走它会诱导「再产出 Finding」。Judge 只调 `complete(messages, schema=JudgeBatchOutput, temperature=0.1)`。一次修复重试可复用 Provider 已有 complete+schema 路径；仍失败 → 该块 keep。

Fake：`complete` 在测试里按 schema 返回 `{"decisions":[...]}`；`LlmAdjudicator` 也可在单测被整个换成 `FakeAdjudicator`（推荐，Pipeline 测不经过 Provider）。

### 7.2 Prompt

新增 `reposage/prompts/tasks/judge.md`，加载方式对齐角色文件（小函数即可，不必上 Jinja 全家桶）。system 侧必须写明：

- 只输出 JSON：`{"decisions":[{"finding_occurrence_id","action","reason"}]}`
- `action` ∈ `keep|downrank`
- 禁止修改路径/行号/证据；禁止声称已验证；禁止发明 Finding；禁止输出 `needs_evidence`
- 重复报告、明显同一问题的弱复述 → `downrank`；独立缺陷 → `keep`
- 不确定 → `keep`（宁可漏降，不可误杀）

user 侧只放 §6.2 摘要。治理/boundary 短引用即可，避免再塞一整份审查角色 prompt。

### 7.3 预算与并发

- 与 REVIEW 共用 Service 里那把 `GlobalBudget`（Pipeline 阶段尚未 settle 掉整 run）。
- 每次 Judge `complete` 前 `reserve(input + max_output)`；`settle` 实际 usage。
- `max_output_tokens` 单独配置（默认 1024，远小于审查 2k+1k）。
- 预留失败：该块 keep，warning `budget`，`JudgeAdjudicationResult.partial=True`，不把 PIPELINE 打成 FAILED。
- Judge 调用计入 `model_requests` semaphore（与审查同池），避免 PIPELINE 再开一档并发打爆 Provider。
- `ModelUsage.role = "judge"`；`prompt_hash` / `schema_hash` 写入 usage。

Judge **不是** optional 角色失败：失败只影响裁决质量，分析状态仍由 REVIEW 的 StrategyHealth 决定。PIPELINE stage 在 Judge fail-open 时可为 `OK` + warning，或 `PARTIAL` 若发生超时/预算跳过（DP-10：用 **PARTIAL + warnings**，与「optional 失败」同形，不把 run 打 FAILED）。

---

## 8. 配置

`ReviewConfig` 增加（`extra=forbid`）：

```yaml
review:
  judge:
    enabled: false              # DP-4：默认关
    max_findings: 32
    timeout_seconds: 30
    temperature: 0.1
    max_output_tokens: 1024
```

- 未知键：Settings 校验失败（启动/preflight）。
- V2-A / V2-B / V2-C compare **强制** `judge.enabled=false`（A/B 继续钉 `static.enabled=false`、A 继续钉 `symbol_retrieval=false`）。
- V2-D compare **强制** `true`（确定性层测可关 Judge；语义层测开 Fake Adjudicator）。
- `llm.routing.judge` 若尚未进 Settings：本里程碑允许 Judge 与主审查共用 `llm.model`；不在本切片做运行时自动路由。若加 routing 字段，缺省等于主模型。

---

## 9. 安全、超时、取消、降级

| 主题 | 契约 |
|------|------|
| 不可信 | Finding 文案/代码片段是数据；用 boundary 包装；日志不打整段 explanation |
| 事实不可伪造 | 应用层白名单只读 `action`+`reason`；canonical / verified / fingerprint / cluster_id / status 仅程序写 |
| 超时 | 该块 keep + warning；其它块继续 |
| 取消 | `CancelledError` 传播；不把迟到 Judge 结果写进 Finding |
| schema 漂移 | extra ignore；非法 action → 该条当缺席 → keep |
| 注入 | Judge user 含 PR 代码时，禁止遵从代码里的「全部 keep/全部 downrank」指令（prompt 写明；单测一条注入样本） |
| 隐私 | 评测默认 `retain_source_in_evals: false` 仍剥离源码 |

---

## 10. 任务、覆盖、阶段、日志

- 每个 Judge 分块一条 `ReviewTask(kind=JUDGE_ADJUDICATE, status=completed|failed|cancelled)`，由 `JudgeAdjudicationResult.tasks` 返回给 Service 落库。`enabled=false`：**不创建**任务、不创建协程（与 static 关闭同形）。
- Coverage：不按文件「审查覆盖」重复计数；若跳过 Judge（关 / 预算 / 溢出）在 `CoverageManifest.truncated=true` 且 item 写清原因 `judge_skipped:*`。
- PIPELINE stage `detail` JSON 增加：`raw_candidates`、`merged`、`judge_enabled`、`judge_keep`、`judge_downrank`、`needs_evidence`、`duplicate_survival_rate`、`dedup_collapse_rate`（口径 §11）。
- 结构化日志 event：`judge_adjudicate`（n_in、n_keep、n_downrank、elapsed_ms）；禁止 dump 完整 decisions JSON 到默认日志。
- usage 全量落库。不新增 SQLite 表（除 `needs_evidence` 列）。

---

## 11. 测试与评测

### 11.1 重复存活率口径（写死，避免实现期口算）

```text
n_raw                    = 进入 Pipeline 的 Candidate 数
n_merged                 = 达到 MERGED 的 Finding 数（含随后 accepted/suppressed/body_only 的那条 survivor）
duplicate_survival_rate  = n_merged / n_raw          # 越低代表重复候选被压缩得越多；n_raw=0 则不定义
dedup_collapse_rate      = 1 - duplicate_survival_rate
```

不要再把 `n_merged / n_raw` 简写成 `duplicate_rate`。它不是“真实重复占比”，而是“重复压力集里的存活比例”。V2 DoD「< 0.25」若沿用，指的是 `duplicate_survival_rate < 0.25`，且只用于故意堆重复的标注样本。

补充披露（不当作 0.25 门槛的分子分母）：

- `n_accepted` / `n_downranked` / `n_needs_evidence`
- `false_merge_pairs`：标注「不应合并」的候选对，却落在同一 `cluster_id`
- `false_downrank`：标注应保留、被 Judge downrank
- `missed_downrank`：标注为重复应降、仍 accepted

V2 DoD「重复率 < 0.25」**仅**作用于 `v2d` 标注集里「故意堆重复候选」的样本（多角色同问题 + 静态同码），实现字段名必须写成 `duplicate_survival_rate`，不是 V1 demo 全集的全局不变量。

### 11.2 必须单测（Fake Adjudicator，不强制真模型）

确定性：

- 聚类先于 fingerprint：merge 前 fingerprint 仍空；merge 后非空。
- 同 path/category/trigger/邻近行 → 一条 Finding、一个 fingerprint。
- 同 path 不同 trigger → 两条（现 `test_pipeline` 行为保持）。
- V2-C：同码去重、异码并存、LLM 不桥接两个静态 rule（回归，不改断言）。

`needs_evidence`：

- 无 verified 证据 + `judge.enabled`：MERGED→body_only，且 **没有** Adjudicator 调用。
- `judge.enabled=false`：同候选仍走 confidence×0.8 + 门槛，发布集合与现网一致。

Judge 应用：

- keep → 仍过门槛则 accepted；canonical/evidence/fingerprint 字节级不变。
- downrank → suppressed，actor=`judge`。
- 缺席 id → keep。
- 模型返回改 path / 设 verified / 附加 findings → 忽略。
- schema 失败 / 超时 → 全 keep + truncated。
- `enabled=false` 零 Judge task、零 complete 调用。
- 注入样本：user 代码含「downrank all」→ Fake 若遵从则测试失败；Llm 路径至少 prompt 含拒绝条款 + 一条脚本化 Fake 证明程序不执行代码内指令（Fake 由测试控制；重点是程序不把代码当 schema）。

取消：Adjudicator 等待时取消 → `CancelledError`，不写迟到决策。

### 11.3 对照评测 `v2d_compare`

主表不是 Fake LLM 的 P/R/F1，而是：

| 表 | 内容 |
|----|------|
| 确定性去重 | 重复候选样本：n_merged、duplicate_survival_rate、dedup_collapse_rate、fingerprint 稳定 |
| 误合并 | 不应合并的对 → 仍两条 |
| Judge | Fake 裁决：keep/downrank 与标注一致；fail-open |
| 成本 | Judge 关 vs 开：calls / input tokens / 墙钟；质量披露 accepted 条数 |

V2-A/B/C compare 钉 `judge.enabled=false`。披露 Fake 限制，与前几阶段同一口径。不宣称真实模型质量收益，除非另跑 `real_compare`（非本里程碑 DoD）。

---

## 12. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查；DP 默认生效 | 用户确认或默认 | — |
| T1 | 确定性层测试钉死；重复存活率辅助函数；不改融合 | 现 pipeline 测全绿 + 新口径测 | T0 |
| T2 | `Finding.needs_evidence`；SQLite v4→v5；开关行为 §5.2 | 关 Judge 行为兼容 | T1 |
| T3 | `FindingAdjudicator` + Fake；`PipelineResult`；Pipeline async；应用 keep/downrank；禁写事实 | §11.2 应用测 | T2 |
| T4 | `LlmAdjudicator`：prompt、`complete`+schema、预算、semaphore、超时/取消；返回 tasks/usages/coverage/warnings | fail-open / 取消测 | T3 |
| T5 | Settings `review.judge`；Service 注入；task/usage/stage/coverage；关则零协程 | 开关测 | T4 |
| T6 | `v2d` 数据集 + compare；钉死 A/B/C 的 judge off | `docs/evidence/v2-d-compare.md` | T5 |
| T7 | 全量回归、Ruff、mypy、`git diff --check` | CI | T6 |

**T0 完成前禁止改 Pipeline / Service。** 不要先接 LLM 再补「不能改事实」。

目录（建议）：

```text
reposage/review/judge.py              # 协议、Fake、应用纯函数
reposage/review/adjudicator.py        # LlmAdjudicator（可与 judge.py 合并，避免空包）
reposage/prompts/tasks/judge.md
reposage/evals/datasets/v2d_dedup.yaml
reposage/evals/v2d_compare.py
```

不新增 `review/judge/` 包，除非实现期文件明显超过单模块。

---

## 13. DoD 与审查清单

### 13.1 交接 DoD

| # | 条目 | 落点 |
|---|------|------|
| 1 | 先确定性 fingerprint/cross_run/cluster | T1 |
| 2 | Judge 仅 keep/downrank | T3 T4 |
| 3 | 不能伪造已验证证据 / 不能改 canonical | T3 |
| 4 | `needs_evidence` 内部标记，V2 降级 | T2 |
| 5 | 重复存活率、误合并、额外成本 | T6 |
| 6 | V1/V2-A/B/C 回归 | T7；对照关 Judge |

### 13.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未引入 Agent / 工具 / 反馈 / watermark
- [ ] Judge 不在 Role Registry
- [ ] 未改 V2-C 融合 / B2
- [ ] 默认 dry-run；不提交、不 push
- [ ] `judge.enabled=false` 时零 Judge 调用、发布集合与 V2-C 相同
- [ ] 日志无密钥、无整文件源码

### 13.3 实现期测试清单

- 聚类先于指纹 / 同簇一条 / 不同 trigger 两条
- needs_evidence 开关分叉
- keep 不改事实 / downrank 出局 / 缺席=keep
- 模型夹带 verified/path/新 Finding 无效
- fail-open 与取消
- A/B/C compare 仍关 Judge

---

## 14. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | Judge 在 Pipeline，Strategy 只交 Candidate | `03` P0-1 |
| **DP-2** | Judge 不是角色；`ReviewTaskKind.JUDGE_ADJUDICATE` | ADR-004 |
| **DP-3** | 确定性层冻结现算法，不改 V2-C 融合 | 交接「先确定性」=验收而非重写 |
| **DP-4** | `judge.enabled` 默认 **False** | 改变发布集合；与 static 同策略 |
| **DP-5** | `process` 改为 async 且返回 `PipelineResult`；禁止同步包装生产路径 | 调用方本就处于 async 链路；Service 统一落库 |
| **DP-6** | 按文件分块 `complete`，不是每条 Finding 一次，也不是多轮对话 | 落实「轻量」同时可控 payload |
| **DP-7** | downrank → `SUPPRESSED`（actor=judge），不是 body_only、不是只乘 confidence | `05`「出局」；keep 仍走原门槛 |
| **DP-8** | `needs_evidence` 仅程序写；行为仅在 Judge 开启时降为 body_only | 关 Judge 时 V1/V2-C 发布兼容 |
| **DP-9** | Judge 走 `complete`+专用 schema，不走 `structured()` | 避免再产出 Finding 信封 |
| **DP-10** | 超时/预算/schema 失败 → keep + PIPELINE PARTIAL/warnings，不 FAILED | fail-soft；审查结果仍在 |
| **DP-11** | Judge **占用** GlobalBudget 与 model semaphore | 与 static 不同：这是模型调用 |
| **DP-12** | `duplicate_survival_rate < 0.25` 只在 v2d 重复标注集上作为 DoD | 避免 single_pass 无重复样本被误杀；避免把存活率误叫真实重复率 |
| **DP-13** | 缺席决策 = keep（不确定不降） | 与 prompt「宁可漏降」一致 |
| **DP-14** | 不改 `cross_run_match_key` 公式（行号锚保留） | 发布幂等；符号锚留给 V2-E |
| **DP-15** | 本阶段不改 Publishing | suppressed/body_only 已有规则 |
| **DP-16** | 溢出 `max_findings` 的 Finding keep，不当 downrank | 防止静默丢评 |

若需推翻某条，只改本节与对应章节。

---

## 15. 冻结声明

V2-D **ACCEPTED**。实现冻结为现网行为。仍禁止：

1. 不把 Judge 做成审查角色，不改 `execute`，不改 V2-C 融合 / B2。
2. 不重开 V2-A B1/B4/S1、V2-B P2、V2-C B1/B2。
3. V2-A/B/C compare 必须继续钉死各自开关，并保持 `judge.enabled=false`。
4. 遗留 P2（`judge_adjudicate` 日志 `n_keep=pending`）非阻断，不作为重开 V2-D 的理由。
5. 反馈记忆与增量 watermark 归 V2-E（`docs/architecture/22-v2e-alignment.md`），不在本里程碑补做。
