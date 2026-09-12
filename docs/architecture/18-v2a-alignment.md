# V2-A 设计对齐（Role Registry + Gate + Barrier）

> 状态：V2-A **ACCEPTED**（见 `docs/evidence/v2-a-review-round6.md`）
> 审查来源：`docs/evidence/v2-a-design-review-round1.md`
> 契约来源：`03` §4–§5、`04` §3/§5–§8、`05` §2 RoleSpec/ReviewTask、`07` §2–§3/§5/§8、`09` §1–§2/§4/§6、`10` §3/§6–§9、`11` §4–§8、`12` §2 V2-a / §7、`14` OQ-5、交接文档 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §7/§9–§10
> DoD 摘要：**Registry 校验通过；门控在标注样本上可说明；三层并发不超限；两阶段准入保证 required 先于 optional 消耗预算；required/optional 经通用 StrategyHealth 归并；GateDecision 全量落库；角色只产出 Candidate；预算超限拒绝新请求；V1 回归通过；产出 V1 vs V2-A 对照评测报告**

---

## 0. 一句话与边界

V2-A 把 V1 的「每文件一次通用审查」升级为「按 diff 特征确定性启用多角色，分层并发，Barrier 归并，共享 `GlobalBudget`」。版本差异只发生在 `ReviewStrategy.execute` 内部；`ReviewService` 骨架、`FindingPipeline`、Publishing、Storage、Provider **不复制、不重写**。

角色 = 一次受控模型调用（role prompt + 已装配的 `ReviewUnit`），**不是** V3 Agent。V2-A **禁止**引入工具循环、无边界推理、静态分析器或 Judge。

**本轮已关闭的实现级缺口（审查 Blocking）**：GateFeatures 如何进入 Strategy、required 两阶段准入、GateDecision 持久化、StrategyHealth 通用归并。

---

## 1. 现有 V1 可复用组件盘点

| 层 | 组件 | V2-A 用法 | 缺口 |
|---|---|---|---|
| 编排 | `ReviewService.review()`：preflight→fetch→context→strategy→pipeline→publish | 保持不变；按 `settings.review.strategy` 选择实现；用 `StrategyHealth` 定稿，**禁止**再看「有无 warning」 | 策略选择器未接 `multi_role`；`finish()` 把任意 warning 打成 `PARTIAL`；`except Exception` 让 `CancelledError` 穿透且不落库 |
| 策略接口 | `ReviewStrategy.execute(units, run, budget) → StrategyResult` | **主签名不变**（Blocking-1：不改 Protocol） | `ReviewUnit` 无 `gate_features`；`SourceRunResult` 无通用归并字段 |
| V1 策略 | `SinglePassReviewer`：file_sem + model_sem、`reserve/settle`、块串行、CancelledError 传播 | 默认策略保留；必须同步填充 `StrategyHealth`（文件失败 = required 失败） | — |
| 上下文 | `ContextAssembler` + `unit_to_messages` | 同一批 `ReviewUnit` 供各角色共用；CONTEXT **一次**从 `ChangedFile` 抽取 `GateFeatures` 写入该文件所有 unit | `unit_to_messages` 无 role 参数；`prompts/` 包空；assembler 尚不产 GateFeatures |
| 流水线 | `FindingPipeline` | **唯一生命周期所有者**；跨角色重复靠现有聚类 | 来源一律 `LLM_GENERAL`；**不做 Judge** |
| 预算 | `GlobalBudget.reserve/settle` | 所有角色共享同一实例；**两阶段准入**保证 required 先预留 | 无两阶段调度；list 提交顺序不能当优先级 |
| 领域 | `RoleSpec` stub、`ROLE_REVIEW`、`ROLE_FAILED`、`LLM_ROLE`、`ModelUsage.role`、`MULTI_ROLE` | 扩展，不另起主流程 | 缺 prompt/budget_weight、`GateFeatures`/`GateDecision`/`StrategyHealth`、`FindingCandidate.role_id` |
| 配置 | `review.strategy` / `concurrency.role_tasks` / `review.roles` | 唯一合并规则见 §3.2 | 启用列表与 spec.enabled / overlay 多重语义未闭合 |
| 存储 | tasks / usages / coverages / run_stages | 现表继续用；**新增 `gate_decisions`（schema v3→v4）** | 无门控审计表 |
| 可观测 | `StructuredLogger`（默认 stderr） | 日志仍打 gate/role 事件，**不能**当唯一审计 | 日志不可查询 |
| LLM | `structured` + Fake | 每角色每 unit 一次；超时见 §11（不读 Protocol 上不存在的属性） | Fake 需按 role 分支 |
| 评测 | `EvalRunner` 可注入 Strategy | 对照复用 | 无门控标注集；无三对照报告 |
| 发布 | Publisher / Saga | V2-A 不改；`publish_status` 不回写分析 status | watermark 增量属 V2-E |

**明确不复用为 V2-A 生产路径**：Static Analyzer（V2-C）、Judge（V2-D）、L3（V2-B）、反馈记忆（V2-E）、Agent loop（V3）。

---

## 2. V2-A 产品目标与非目标

### 2.1 目标（FR-14 / FR-15 的本切片）

1. 按 **diff 特征确定性门控** 启用角色，未命中则零调用、零成本。
2. 首批角色可运行、可配置、可关闭；默认不自动把用户从 `single_pass` 升级到 `multi_role`。
3. 文件 / 角色 / 模型三层 Semaphore 同时生效；**run 级两阶段准入**（先 required 后 optional）；所有 LLM 调用共享 `GlobalBudget` 原子预留。
4. 每阶段内 Barrier 等待本批已选角色；required/optional 失败经 **StrategyHealth** 归并，Service 不解析 `path::role`。
5. 任何角色只返回 `FindingCandidate`；正式 Finding 仍只由 `FindingPipeline` 推进。
6. 每个已调用的 `(file, role)` 落库 ReviewTask / Usage / CoverageItem；**每个被评估的 `(file, role)` 落库 GateDecision**（含 miss）。
7. 门控有标注评测（分角色门槛）；与 V1 single-pass 做质量 / 成本 / 延迟三对照。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| L3 符号检索 / Tree-sitter | V2-B | 角色仍用 V1 的 L0–L2+L4 |
| ruff/静态分析融合 | V2-C | `04` 总图有 STAT，V2-A 时序删除该 par |
| Judge keep/downrank、语义去重增强 | V2-D | V1 聚类+fingerprint 已够用；重复率只观测不优化 |
| 反馈记忆、增量 watermark 审查 | V2-E | 发布 Saga 保持 V1-E 行为 |
| 工具、Agent loop、会话压缩 | V3 | 角色 ≠ Agent（ADR-004） |
| 复制一套 V2 主流程 | — | `12` §7 不重写承诺 |
| 角色数量膨胀 | 后续切片 | 交接：不要过早膨胀；DP-1 已确认四角色 |
| 用模型做门控 | — | `09` §4：分类/门控确定性优先 |
| 改 `ReviewStrategy.execute` 主签名 | — | Blocking-1：避免 SinglePass/EvalRunner 连锁改动 |
| 优先级调度器 / required 预留水位 | 可后置 | V2-A 用两阶段准入，不做复杂调度器 |

### 2.3 与架构原文的已知差异（必须显式，不静默改契约）

| 架构原文 | V2-A 裁决 | 处理 |
|----------|-----------|------|
| `07` §2 多角色清单 | 交接四角色 | **DP-1 确认**：`general/security/correctness/performance`；silent-failure/edge-case 并入 correctness；其余注册但默认不评估 |
| `11` §8 V2 DoD 写 silent-failure | 随 DP-1 | 评测 category 仍用现有 `FindingCategory` |
| `04` §3 含静态分析 par | V2-A 不含 | 文档切片 |
| `07` §5 Judge | V2-A 不启用 | Pipeline 保持 V1 置信度门槛 |
| `RoleSpec.gate` 为 Callable | `gate_id: str` | 函数在 Registry 绑定 |
| `04` §8「角色失败 → PARTIAL」 | 仅 required 失败 → PARTIAL | P1-R2-4 精确化，见 §8 |

---

## 3. 领域模型

### 3.1 `RoleSpec`（扩展现有 stub）

| 字段 | 类型 | 约束 |
|------|------|------|
| `id` | str | Registry 唯一 |
| `prompt_id` | str | `prompts/roles/<id>.md`；变更走 content_hash |
| `gate_id` | str | 纯函数键；`always` = 只过语言门 |
| `model_requirement` | str | V2-A 全部主模型档 `general` |
| `enabled` | bool | **builtin 默认值**；有效启用见 §3.2，禁止与 `review.roles` 双源打架 |
| `required` | bool | builtin：`general=true`，其余 `false`；降级禁令见 §3.2 |
| `budget_weight` | float | `>0`，默认 1.0；V2-A **不**缩放 max_output（DP-4） |
| `languages` | list[str] | 空 = 跟随 `review.languages` |

**不做**：用户 lambda 门控。

### 3.2 `RoleRegistry`：唯一合并规则（should-fix 6.5）

配置层级与现网 Settings 一致，**角色字段不得另搞一套优先级**：

```text
builtin RoleSpec
  < 用户配置 ~/.reposage.yaml
  < 受信任仓库配置 reposage.yaml（默认分支 / 本地仓库文件）
  < CLI/Action 显式 overrides
```

PR 分支中的 `reposage.yaml` **不读取**（`09` §6 铁律）：不能加角色、不能把 `required` 降级、不能把 `strategy` 改成更贵的模式。

**有效启用（唯一公式）**：

```text
effective_enabled(id) =
    id 已注册
    AND id ∈ settings.review.roles
    AND overlay(id).enabled is not False
```

- 不在 `review.roles` 里的 id：即使 builtin `enabled=true` 也不评估、不调用、不写 per-file GateDecision。
- 在 `review.roles` 里且无 overlay：启用。
- overlay `enabled=false`：从允许名单剔除（允许「列表暂留、开关关掉」）。
- 禁止出现「`review.roles` 含 id 且 overlay 未关，但 spec.enabled=false 导致结果不明」——`spec.enabled` 只是 builtin 默认，被上面公式覆盖。

**required 合并**：

```text
effective_required(id) = builtin.required OR overlay.required
```

- 只允许 **升级**（optional → required）。
- **禁止降级**：`general.required` builtin 为 true 时，任何受信任配置/CLI 都不能改成 false；非法配置 → preflight FAILED。

**其它校验**：`review.roles` ⊆ 已注册 id；至少一个 `effective_enabled ∧ effective_required`；未知 id / 重复 id / `budget_weight≤0` → preflight FAILED。

`review.strategy` 显式为 `multi_role` 才构造 `MultiRoleReviewer`；默认 `single_pass` 与 V1 字节级兼容（DP-7）。

默认 `review.roles: [general]`。用户打开 multi_role 时应显式列出四角色，例如 `[general, security, correctness, performance]`；文档与示例配置写明，不靠「strategy=multi_role 自动膨胀名单」。

### 3.3 `GateFeatures` / `GateDecision` 与进入 Strategy 的契约（Blocking-1 / DP-8）

**选定方案：`ReviewUnit.gate_features: GateFeatures | None = None`。**

不改 `execute(units, run, budget)` 主签名。不引入 `FileReviewBundle`。Gate **禁止**反向解析 prompt / UNTRUSTED 文本。

`GateFeatures`（纯数据，由 `ChangedFile` 确定性抽取，只看 **added** 行）：

| 字段 | 来源 |
|------|------|
| `path` / `language` / `status` | `ChangedFile` |
| `path_parts` | 路径分段 |
| `added_imports` | 新增 import 行 |
| `keyword_hits` | §5.2 词族 |
| `combo_hits` | 组合命中（如 `path_io+user_input`），供收紧后的 security 使用 |
| `has_added_lines` | 纯删除/无新增 |

`GateDecision`：

| 字段 | 含义 |
|------|------|
| `role_id` / `file_path` | 决策对象 |
| `enabled` | 是否调用模型 |
| `matched_features` | 命中的 path/import/keyword/combo |
| `reason` | `always` / `gate_hit` / `gate_miss` / `lang_miss` / `no_added_lines` |
| `gate_version` | 规则集 content_hash（还原旧决策） |

`disabled` 不作为 per-file reason：未进入 `review.roles` 的角色根本不评估。

**数据流**：

```text
CONTEXT（ReviewService，仍禁止 Strategy 重做 diff）：
  对每个 kept ChangedFile 调用 extract_features(file) 一次
  写入该文件全部 ReviewUnit.gate_features（同文件多 unit 值相等）
SinglePass：忽略该字段（默认 None 时 V1 路径不变；有值也不读）
MultiRole：按 file_path 分组，取该组任一 unit.gate_features
  若缺失 → 视为实现错误（CONTEXT 未盖戳），该文件 required 记失败，不解析 prompt 补救
```

`extract_features` 放在 `review/reviewers/roles/gates.py`（或 `review/gates.py`），由 Service 的 CONTEXT 调用，**不是** Strategy 私货。这样 Fake/评测只要走 assembler 盖戳即可。

### 3.4 `RoleTaskResult` 与 Gate 落库（Blocking-3 / DP-3 修订）

一次 **实际调用** 的 `(file, role)` 内部结果，不进入 Finding 生命周期：

| 字段 | 类型 |
|------|------|
| `task` | `ReviewTask(kind=role_review, target="{path}::{role_id}")` |
| `candidates` | `list[FindingCandidate]`（已填 `role_id`） |
| `usages` | `list[ModelUsage]` |
| `gate` | `GateDecision`（enabled=true 的那条） |
| `coverage` | `CoverageItem(target="{path}::{role_id}", reason=covered\|role_failed\|truncated\|task_failed)` |

**DP-3 修订**：

- Gate miss / lang_miss / no_added_lines：**不创建 ReviewTask，不写 CoverageItem**（不是失败覆盖）。
- **全部**被评估的 GateDecision（含 miss）写入 `gate_decisions` 表，键 `(run_id, file_path, role_id)` 幂等 upsert。
- stderr JSONL 仍打 `gate_decision` 事件，只是辅助，不是审计源。

### 3.5 `FindingCandidate` 兼容扩展

`role_id: str | None = None`。V1 不填 → Pipeline `LLM_GENERAL`。MultiRole 必填 → `LLM_ROLE` + `role_id`。

### 3.6 `StrategyHealth` 通用归并契约（Blocking-4 / DP-10）

扩展 `SourceRunResult`（所有 Strategy 共用，默认值保持 V1 可编译）：

```text
class StrategyHealth:
    required_failed: bool = False
    required_failure_count: int = 0
    optional_failure_count: int = 0
    coverage_complete: bool = True   # 每个 kept 文件的 required 工作已成功完成
```

`SourceRunResult.health: StrategyHealth`

Service **只**根据 health + 异常类型定稿，禁止：

- 扫描 `warnings` 决定 PARTIAL；
- 解析 `task.target` 的 `::`；
- 反向查询 RoleRegistry 判断某个 task 是否 required。

`SinglePassReviewer` 必须填充：任一 `file_review` 任务失败 → `required_failed=True`，`required_failure_count+=1`，`coverage_complete=False`。V1「单文件失败 → PARTIAL」经同一契约保留。CONTEXT 过滤 skip **不**进入 Strategy，故不把 skip warning 算成 required 失败。

### 3.7 枚举与存储

- **不新增** `StageName`。
- `CoverageReason` 仍只用于已选角色的 covered/role_failed/truncated/task_failed；**不**增加 `gate_miss`（miss 走 `gate_decisions`）。
- schema **v3→v4**：新建 `gate_decisions`（见 §10）。旧库 `IF NOT EXISTS` + `user_version=4`。

---

## 4. `MultiRoleReviewer` 与 `ReviewStrategy` 的关系

```text
ReviewService
  ├─ strategy 选择（显式配置，禁止自动升级）
  │     single_pass → SinglePassReviewer     （V1，默认）
  │     multi_role  → MultiRoleReviewer      （V2-A）
  │     agentic     → 拒绝（V3 未实现）
  ├─ CONTEXT 盖戳 ReviewUnit.gate_features
  ├─ execute(units, run, budget)  → StrategyResult
  │     含 SourceRunResult.health
  └─ FindingPipeline.process(candidates)
```

| 规则 | 说明 |
|------|------|
| 同一接口 | `execute` 三参数不变；`supports(run.strategy is MULTI_ROLE)` |
| 同一输入 | CONTEXT 已装配的 `ReviewUnit[]`（现带 `gate_features`）；不重做 diff |
| 同一输出 | `StrategyResult(candidates, SourceRunResult{tasks, usages, warnings, health, gate_decisions})` |
| `gate_decisions` | 放在 `SourceRunResult` 上，Service 调 `storage.record_gate_decisions`；SinglePass 交空列表 |
| 禁止 | 创建/推进 Finding；Publisher；watermark；工具；Agent；从 prompt 文本抽门控特征 |
| 粒度 | V2-A：`kind=role_review`，一文件×一**已调用**角色一任务 |
| 失败隔离 | 单角色失败不影响同阶段其他角色；CancelledError 不转 role_failed |

`run.strategy` 在 execute 前写成实际策略名。

---

## 5. 门控特征表与首批角色规则

### 5.1 语言门

`file.language in spec.languages`（空则 `review.languages`）。CONTEXT 已过滤不支持语言；Strategy 再防一层 → `lang_miss`。

### 5.2 特征词族（added 行正文；忽略纯 `#` 注释与 docstring 行）

| 词族 | 示例 | 角色 |
|------|------|------|
| `exec_dyn` | `eval(` `exec(` `compile(` | security |
| `cmd` | `os.system` `subprocess` `shell=True` `os.popen` | security |
| `deser` | `pickle.loads` `yaml.load(` `marshal` | security |
| `web_io` | `requests.` `httpx.` `urllib` `urlopen` | security |
| `auth` | `jwt` `oauth` `password` `api_key` `os.environ` | security |
| `sql` | `execute(` `executemany` f-string SQL | security |
| `secret_lit` | 硬编码 key/token（复用 `python.05`） | security |
| `user_input` | `request.args/files/form`、`UploadFile`、`werkzeug`、未校验 `filename` | security 组合 |
| `path_io` | `open(` `Path(` `os.path` `pathlib` | **不足单独启用 security**（should-fix 6.4） |
| `except_swallow` | `except:` / `except Exception` + `pass`/`continue` | correctness |
| `async_fire` | `asyncio.create_task` `add_done_callback` 无 await | correctness |
| `none_index` | 裸 `assert`、明显空值/下标假设 | correctness |
| `loop_nested` | 双层 `for`/`while`、`itertools.product` | performance |
| `heavy_collect` | 循环内同步 IO、无界 list 增长 | performance |
| `path_auth` | `auth/` `security/` `*_view.py` `views.py` | security |
| `path_perf` | `cache/` `batch/` `worker/` | performance |

门控命中 ≠ Finding。`path_io` 单独命中只记入 `keyword_hits`，**不**产生 security `gate_hit`。

### 5.3 首批角色规则（DP-1 确认）

| id | required | gate | 停止 |
|----|----------|------|------|
| `general` | **true** | `always`（过语言门） | 空列表合法 |
| `security` | false | `exec_dyn\|cmd\|deser\|web_io\|auth\|sql\|secret_lit\|path_auth` **或** combo `path_io AND (user_input\|web_io\|auth\|path_auth\|upload/extract 路径)` | miss → 不调用 |
| `correctness` | false | except_swallow / async_fire / none_index / 裸 assert | 同上 |
| `performance` | false | loop_nested / heavy_collect / `path_perf` | 同上 |

占位注册、默认不在 `review.roles`：`silent-failure`、`edge-case`、`concurrency`、`test`。

### 5.4 变更类型

| status | 行为 |
|--------|------|
| added / modified | 正常抽特征 |
| renamed（有新增行） | 新路径 + 新增行 |
| deleted / 无新增 | `no_added_lines`：optional miss；`general` 仍调用 |
| 二进制/生成/超限 | CONTEXT skip，Strategy 不可见 |

### 5.5 门控伪代码

```text
features = unit.gate_features          # 已由 CONTEXT 盖戳
for spec in registry.effective_enabled():
    d = gate(features, spec, languages)
    persist d                          # 无论 hit/miss
    if d.enabled:
        selected.append(spec)
```

---

## 6. 三层并发、两阶段准入与 Barrier

### 6.1 三层 Semaphore

| 层 | 配置 | 保护 |
|----|------|------|
| file | `concurrency.file_tasks`（3） | 同时处理的文件组 |
| role | `concurrency.role_tasks`（3） | 同时执行的 `(file, role)` |
| model | `concurrency.model_requests`（3） | 在途 LLM 请求 |

最坏在途 LLM ≤ `model_requests`。锁序固定：

```text
file_sem → role_sem → model_sem → budget.reserve() → llm.structured()
```

`reserve` 必须在拿到 `model_sem` 之后（与 V1 相同）。

同一 `(file, role)` 多 unit **串行**。

### 6.2 两阶段准入（Blocking-2 / DP-9 确认）

**不**把「先 append required 再 gather」当作优先级。V2-A 采用可证明的 run 级两阶段：

```text
Gate：对所有 kept 文件 × effective_enabled 角色先算完 GateDecision（无 LLM）

Phase 1 — required
  仅调度 effective_required 且 gate.enabled 的任务（V2-A 即每文件 general）
  文件间 file_sem 并发；角色仍走 role_sem + model_sem
  每文件 barrier：等该文件 required 结束
  Barrier-required：等全部 kept 文件的 required 结束
  在此之前禁止启动任何 optional LLM 调用

Phase 2 — optional
  仅调度 gate.enabled 的 optional
  同样三层 sem + 每文件 barrier
  Barrier-optional：等全部 optional 结束（或预算拒导致不再发新请求）
```

Phase 1 未完成时 Phase 2 的 coroutine **不创建**。optional 可以在 Phase 1 结束时才根据剩余预算决定是否 `reserve`。

代价：整体并发度低于「全角色一张 gather」，但 required 消耗与测试可证明。压力测试见 §14.3。

不做优先级调度器、不做 required 预留水位（非目标）。

### 6.3 Barrier 语义（DP-2：阶段内仍 per-file）

| 规则 | 行为 |
|------|------|
| 等待对象 | 本阶段本文件已调度角色；未启用的不等 |
| optional 失败 | RoleTask=failed + warning + coverage.role_failed；不取消兄弟 |
| required 失败 | fail-soft 等完本文件本阶段其他 required；计入 health |
| CancelledError | 立即重新抛出，不转 role_failed |
| Barrier 职责 | 聚合 Candidate / Task / Usage / Coverage；不做 schema/Judge |

### 6.4 时序

```text
SVC CONTEXT 盖戳 gate_features
SVC → MR.execute
MR 计算并收集全部 GateDecision
Phase1 required（file_sem）→ Barrier-required
Phase2 optional（file_sem）→ Barrier-optional
reduce → StrategyResult(+ health + gate_decisions)
SVC 落库 gate_decisions + tasks + usages
SVC 按 health 定稿 → Pipeline → publish
```

### 6.5 背压

Semaphore 排队。墙钟到期 `reserve` 返回 None。在途用 `asyncio.timeout(budget.remaining_runtime_seconds)` 取消；迟到结果不入 findings。

---

## 7. `GlobalBudget` 在多角色下的原子预留

单一 `GlobalBudget`，不按角色拆池。`budget_weight` V2-A 只落字段默认 1.0，**不缩放** `max_output`，**不**参与排序（两阶段已经表达 required 优先）。

| 场景 | 行为 |
|------|------|
| `reserve()` 成功 | 发请求；`settle(实际)` |
| Phase 1 `reserve` None | required RoleTask failed + truncated；`health.required_failed=True`；`coverage_complete=False` |
| Phase 2 `reserve` None | optional failed + truncated + warning；`optional_failure_count+=1`；不放大 Run |
| retry / schema repair | 重试前再次最坏预留 |
| 未定价 | `pricing_status=unknown`；token/墙钟仍硬顶 |
| overrun | 熔断后续所有角色（含尚未开始的 Phase 2） |

`reserved_finalize_ratio` 仅 V3；V2-A 角色审查吃满 `max_total_tokens`。

成本模型：最坏 ≈ `Σ Phase1 required + Σ Phase2 selected optional`。评测报告门控跳过次数与两阶段拒绝次数。

---

## 8. required / optional 失败矩阵与 Service 定稿

### 8.1 Service 定稿（唯一入口）

```text
CancelledError → finish(CANCELLED) → record_run（含已有 stages）→ raise
其它未捕获异常 → finish(FAILED) → record_run → raise

成功返回 StrategyResult 后：
  publish 只写 publish_status，不改分析 status
  health.required_failed OR NOT health.coverage_complete → PARTIAL
  仅 optional_failure_count>0                       → COMPLETED + 已有 warnings
  全成功                                            → COMPLETED

CONTEXT skip / 过滤说明可以进 run.warnings，但不得单独把分析状态打成 PARTIAL。
```

普通 skip warning 是否降级：**否**。这是对「有 warning 就 PARTIAL」的显式废除。

### 8.2 矩阵

| # | 条件 | Task | Coverage | gate_decisions | health | Run.status |
|---|------|------|----------|----------------|--------|------------|
| 1 | optional gate miss | 无 | 无 | enabled=false, reason=gate_miss | 不变 | 不影响 |
| 2 | 角色成功、0 候选 | completed | covered | hit/always | 不变 | 不影响 |
| 3 | optional LLM/JSON 失败 | failed | role_failed | hit | optional_failure_count++ | required 全成功 → **COMPLETED** |
| 4 | required LLM/JSON 失败 | failed | role_failed | always | required_failed | **PARTIAL** |
| 5 | optional 预算拒（Phase 2） | failed | truncated | hit | optional_failure_count++ | COMPLETED |
| 6 | required 预算拒（Phase 1） | failed | truncated | always | required_failed；coverage_complete=false | **PARTIAL** |
| 7 | general 成功 + security 失败 | 各记 | 各记 | 两条 | optional_failure_count=1 | **COMPLETED** |
| 8 | 文件 A general 失败 | A failed | role_failed | always | required_failed | **PARTIAL** |
| 9 | required 全成功，若干 optional 失败 | — | — | — | optional_failure_count>0 | **COMPLETED** |
| 10 | 外部取消 | 可能未返回 | — | 已算的决策仍应尽量落库 | — | **CANCELLED** 后 raise |
| 11 | 墙钟到、在途取消 | 视 required | truncated/role_failed | — | 同 4 或 5 | 同 4/5 |
| 12 | 空 diff / 全部过滤 | 无 REVIEW 调用 | 过滤项 | 无 per-file 行 | coverage_complete=true（无 kept） | **COMPLETED** |
| 13 | Registry/配置非法 | 不启动 | — | 无 | — | **FAILED** preflight |
| 14 | 空列表 | 同 2 | covered | — | 不变 | 空 ≠ 失败 |

REVIEW `StageResult.required=true`。阶段 status：`health.required_failed` → `partial`；仅 optional 失败 → `ok` + detail 计数。

### 8.3 取消持久化（should-fix 6.1）

V2-A **闭环** Run 状态落库，而不是把 CANCELLED 后置：

1. `review()` 将 `CancelledError` 与普通 `Exception` 分开捕获（`CancelledError` 是 `BaseException`，现网 `except Exception` 本来就不会吞掉它，但也不会落库）。
2. 捕获后：`run.finish(CANCELLED)`，当前阶段 `StageResult.status=failed`（detail=`cancelled`），`record_run`，日志 `event=run_cancelled`，**然后重新 raise**。
3. 若取消发生在 `execute` 返回前：可能没有完整 tasks；不编造。若 Phase 1 已返回内部缓冲，允许尽最大努力把已有 tasks/usages/gate_decisions 写入（实现时用 `try/finally` 在 Strategy 内不吞取消，Service 侧只能记录 run）。**最低要求**：run 行 status=`cancelled` 可查。
4. DoD 宣称「CANCELLED 落库」= 上述最低要求 + 不吞取消；不宣称取消时所有 in-flight LLM 的 Usage 完整。

### 8.4 与发布

分析 `COMPLETED` + optional warning 不阻止发布。发布失败只改 `publish_status`。

---

## 9. Prompt 分层与角色边界

### 9.1 组合

```text
system = L0 + role.<id> + L4 + 固定输出协议
user   = UNTRUSTED(file_path JSON) + L1 + L2
```

仓库字符串禁止进 role 文件。`unit_to_messages(unit, *, role_prompt: str | None = None)`；`None` 时与 V1 字节兼容。

### 9.2 边界

| 角色 | 必须 | 禁止 |
|------|------|------|
| general | 明显正确性/会炸的 API 误用 | 纯风格；不要扫一遍 security 清单 |
| security | 注入/鉴权/反序列化/密钥/路径穿越 | 风格；无数据流的「看起来像」 |
| correctness | 异常吞噬、错误丢失、assert 当校验、空值/边界 | 性能品味；未证明安全漏洞 |
| performance | 明显 O(n²)/热路径同步 IO | 微优化 |

无问题输出空列表，不算失败。

### 9.3 模型路由

同一 `LLMProvider`。不自动换模。temperature=0.1；修复重试 1 次。请求超时在 **Provider 内部**用 `Settings.llm.timeout_seconds`，不从 Protocol 读取。

---

## 10. 落库方案

| 数据 | 约定 | 结构 |
|------|------|------|
| `ReviewTask` | `task_id="{run_id}:{path}::{role_id}"`；仅已调用角色 | 现表 |
| `ModelUsage` | `role=role_id` | 现表 |
| `CoverageItem` | 仅已调用角色 | 现 JSON |
| `run_stages` | REVIEW 一条；detail JSON 含 required_failed / optional_failed | 现表 |
| **`gate_decisions`** | 每个被评估的 `(run, file, role)` 一行 | **新表，v3→v4** |
| `SourceRunResult.gate_decisions` | Service `record_gate_decisions` | Storage 协议新增 |
| 日志 | 辅助；含 `gate_decision` 事件；禁源码/prompt | stderr JSONL |

`gate_decisions` 草案：

```text
run_id TEXT NOT NULL REFERENCES runs(run_id)
file_path TEXT NOT NULL
role_id TEXT NOT NULL
enabled INTEGER NOT NULL
reason TEXT NOT NULL
matched_features_json TEXT NOT NULL DEFAULT '[]'
gate_version TEXT NOT NULL
PRIMARY KEY (run_id, file_path, role_id)
```

幂等 upsert。`gate_version` = 门控规则集 hash。Gate eval 可直接读该结构（测试也可用内存列表，不必先写盘）。

覆盖合并：CONTEXT skip + REVIEW 已调用角色项。miss 不进入 CoverageManifest。

---

## 11. 安全、隐私、超时、取消和降级

| 主题 | V2-A 行为 |
|------|-----------|
| 注入 | 角色 prompt 可信；代码 UNTRUSTED。新增行堆 `eval(` 仍可能启用 security；缓解：忽略注释行、三层 sem、硬预算、两阶段、path_io 不单独触发 |
| 幻觉 | Pipeline 重定位 |
| Secret | 日志/评测/对照脱敏 |
| 路径 | 特征只用 `ChangedFile`；不新读盘 |
| **超时** | **沿用 V1**：Strategy `async with asyncio.timeout(budget.remaining_runtime_seconds)` 管全局剩余墙钟；Provider 内部用 Settings 的 `timeout_seconds` 管单次 HTTP。**禁止** `llm.timeout_seconds` 这种 Protocol 上不存在的属性（should-fix 6.2） |
| 取消 | 不吞 `CancelledError`；Service 落库 CANCELLED 再 raise（§8.3） |
| 降级 | JSON 修复 1 次 → 丢弃该 unit；超大 PR 走 V1 skip；预算拒停新请求，已有候选进 Pipeline |
| 分支配置 | 不读 PR 分支 yaml |
| dry-run | 默认；不自动发布 |
| 隐私 | 评测默认识剥源码 |

疑似注入不作为代码 Finding。

---

## 12. 门控评测数据集和指标

### 12.1 两套评测

| 套件 | 测什么 | 指标 |
|------|--------|------|
| Gate eval | 纯函数门控 | 分角色 Precision/Recall；每条 miss 绑定词族 |
| Quality eval | 端到端 Finding | `11` §5 + 成本/延迟/角色调用次数/两阶段拒绝次数 |

`general` 不计入 Gate Precision。

```text
Gate Precision_r = 角色 r 正确启用次数 / 角色 r 实际启用次数
Gate Recall_r    = 角色 r 正确启用次数 / 标注应启用次数
```

### 12.2 分角色门槛（DP-5 修订；should-fix 6.3）

「宁漏叫不可乱花」只适用于 **performance**（偏成本）。security 相反，优先不漏。

| 角色 | Precision | Recall | 倾向 |
|------|-----------|--------|------|
| security | ≥ 0.6 | ≥ 0.8 | 优先 Recall |
| correctness | ≥ 0.7 | ≥ 0.7 | 平衡 |
| performance | ≥ 0.8 | ≥ 0.6 | 优先 Precision |

不与 Finding Precision 混用。path_io 单独样本必须 **不**启用 security（负例）。

### 12.3 样本构成（≥16）

| 类 | 例 | 期望 |
|----|----|------|
| security 真值 | eval / pickle / os.system / f-SQL / 硬编码密钥 | general+security |
| security 组合 | handler 里 `open(user_filename)` | general+security |
| path_io 负例 | 普通 `Path("config.json").read_text()` 无用户输入 | **仅 general** |
| correctness 真值 | `except Exception: pass`、裸 assert、create_task 无 await | general+correctness |
| performance 真值 | 双层循环 + 循环内同步 IO | general+performance |
| 负例 | 纯重命名、注释里写 eval、docstring | 仅 general |
| 路径门 | `auth/session.py` | general+security |
| 删除 | 无 added | 仅 general |

### 12.4 V1 vs V2-A 对照

质量 / 成本 / 延迟三表强制。脚本化 Fake 与真实 API 分列。不要求全局 Precision 超过 V1；security/correctness Recall 不下降或有说明。损 Precision → 记入报告，交给 V2-D，本里程碑不用模型去重修补。

### 12.5 回归

V1 全量 pytest + Ruff + mypy。`single_pass` 不得改 task kind / 来源 kind；`StrategyHealth` 填充不得改变 V1 单文件失败 → PARTIAL 的对外语义。

---

## 13. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 对齐稿第二轮复验；DP-1～DP-10 按本节默认 | 用户确认或默认生效 | — |
| T1 | 领域：`GateFeatures`/`GateDecision`/`StrategyHealth`；`ReviewUnit.gate_features`；`FindingCandidate.role_id`；`SourceRunResult` 扩展 | mypy；SinglePass 旧测绿 | T0 |
| T2 | 配置唯一合并公式 + required 禁止降级 + 未知角色失败 | `tests/test_config.py` | T1 |
| T3 | `RoleRegistry` 四角色 + 占位 id | 单元 | T2 |
| T4 | CONTEXT 盖戳 `extract_features`；gates 纯函数；**path_io 负例**；门控 YAML | Gate 分角色 P/R 可打印 | T3 |
| T5 | `prompts/roles/*.md` + `unit_to_messages(..., role_prompt=)` | SinglePass 不传则快照不变 | T1 |
| T6 | `MultiRoleReviewer`：三层 sem、**两阶段准入**、per-file barrier、reserve/settle | in-flight ≤ model_requests；**压力：预算只够 required 时 optional 零消耗且 required 全完成** | T4 T5 |
| T7 | Pipeline `role_id` → `LLM_ROLE` | V1 来源不变 | T1 |
| T8 | Service：策略选择器；按 **health** 定稿；CANCELLED 落库再 raise；CONTEXT 盖戳；`record_gate_decisions` | 矩阵 §8；skip warning 不 PARTIAL；publish_status 独立 | T6 T7 T9a |
| T9 | 日志 event 与脱敏 | `test_logging` | T8 |
| T9a | Storage v3→v4 `gate_decisions` upsert + 协议 | 迁移测；幂等主键 | T1 |
| T10 | Gate runner + V1 vs V2-A 三对照 | `docs/evidence/v2-a-compare.md` | T8 |
| T11 | 全量回归、Ruff、mypy、`git diff --check` | CI | T10 |

**T0 完成前禁止 T6。** 不要跳到 T6 先写 MultiRoleReviewer。

目录：

```text
reposage/review/reviewers/roles/registry.py
reposage/review/reviewers/roles/gates.py      # extract_features + gate 纯函数（CONTEXT 也可 import）
reposage/review/reviewers/roles/multi_role.py
reposage/prompts/roles/{general,security,correctness,performance}.md
reposage/evals/datasets/v2a_gate.yaml
```

`review/single_pass.py` 不搬家；但必须填 `health`。

---

## 14. DoD 与审查清单

### 14.1 交接 DoD

| # | 条目 | 落点 |
|---|------|------|
| 1 | Registry 加载与配置验证 | T2–T3；唯一合并公式 |
| 2 | Gate 可说明命中率 | T4/T10；分角色门槛 |
| 3 | 三层并发不超限 | T6 |
| 4 | required/optional 失败语义 | StrategyHealth + T8；非 warning 启发式 |
| 5 | 只输出 Candidate | 不变 |
| 6 | 预算超限拒绝新请求 | reserve + 两阶段压力测 |
| 7 | V1 回归 | T11 |
| 8 | 对照报告 | T10 |
| 9 | GateDecision 可查询 | T9a |
| 10 | CANCELLED 落库且不吞取消 | T8 |

### 14.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未复制主流程
- [ ] 未引入 Agent / 工具 / L3 / ruff / Judge / 反馈记忆
- [ ] 未用 gather 提交顺序冒充 required 优先
- [ ] Gate 不解析 prompt 文本
- [ ] 默认 dry-run / single_pass
- [ ] 对照三表；旧样本不消失

### 14.3 实现期测试清单

- Registry 合并与 required 降级拒绝
- 门控：每角色 1 正 1 负；注释骗门控；**单独 open()/Path() 不启用 security**
- `ReviewUnit.gate_features` 同文件一致；SinglePass 忽略
- 三层 sem；锁序
- **两阶段压力：token 预算只够全部 general，N 个文件均 gate_hit security → security 调用次数 0，general 全 completed**
- 矩阵 3/4/7/9/10/12
- skip warning → COMPLETED
- health 驱动 PARTIAL，不解析 target
- CancelledError：DB 中 run.status=cancelled 且异常继续抛出
- gate_decisions 主键幂等；miss 有行、无 task
- V1 SinglePass 黄金路径

---

## 15. 决策点（第一轮审查建议；未遭用户反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | **确认** general/security/correctness/performance | 角色数适中；correctness 吸收 silent-failure/edge-case |
| **DP-2** | **确认** 阶段内 per-file Barrier | 与 V1 map-reduce 一致；外层再加 run 级 Barrier-required/optional |
| **DP-3** | **修订** miss 不建 Task/CoverageItem，但 GateDecision 全量落库 | 审计与任务语义分离 |
| **DP-4** | **确认** 不缩放 max_output | 降低预算复杂度 |
| **DP-5** | **修订** 分角色门槛：security 偏 Recall，correctness 平衡，performance 偏 Precision | 风险与成本不同 |
| **DP-6** | **确认** 只有 general required | optional 失败不降级 |
| **DP-7** | **确认** 默认仍 single_pass | 对照、成本、V1 兼容 |
| **DP-8** | **确认** `ReviewUnit.gate_features`；不改 execute 签名 | Blocking-1 推荐方案 |
| **DP-9** | **确认** run 级两阶段准入，不做优先级调度器 | Blocking-2；可证明 |
| **DP-10** | **确认** `StrategyHealth` 通用归并；SinglePass 同步填充 | Blocking-4 |

若需推翻某条，只改本节与对应章节，不重开架构。

---

## 16. 审查通过前的冻结声明

1. 不新增 `MultiRoleReviewer` 生产代码、不改默认策略、不把 V1 任务改成 `role_review`。
2. 不在 Pipeline 启用 Judge，不接静态分析，不接 L3。
3. 不提交、不 push、不打 tag。
4. 设计复验通过后从 T1 起实现，**禁止跳到 T6**。

---

## 17. 第一轮审查修订登记

对象：`docs/evidence/v2-a-design-review-round1.md`。

| 编号 | 结论 | 修订位置 |
|------|------|----------|
| Blocking-1 Gate 输入 | 采纳：`ReviewUnit.gate_features`；CONTEXT 盖戳；不改 execute 签名 | §3.3、§4、DP-8、T1/T4 |
| Blocking-2 required 优先 | 采纳：run 级两阶段准入 + 压力测试 | §6.2、§7、DP-9、T6 |
| Blocking-3 门控审计 | 采纳：`gate_decisions` 表；miss 不建 Task | §3.4、§10、DP-3、T9a |
| Blocking-4 归并契约 | 采纳：`StrategyHealth`；Service 不解析 MultiRole 细节；SinglePass 同步填 | §3.6、§8.1、DP-10、T8 |
| 6.1 CANCELLED | 采纳：落库再 raise；最低要求写清 | §8.3、T8 |
| 6.2 超时口径 | 采纳：墙钟用 budget timeout；HTTP 超时留 Provider/Settings | §11 |
| 6.3 指标文案 | 采纳：分角色门槛，取消「宁漏叫」一刀切 | §12.2、DP-5 |
| 6.4 path_io | 采纳：单独 open/Path 不足启用 security | §5.2–5.3、§12.3 |
| 6.5 Registry 合并 | 采纳：唯一公式 + required 禁止降级 | §3.2 |
| DP-1/2/4/6/7 | 按审查建议确认 | §15 |
