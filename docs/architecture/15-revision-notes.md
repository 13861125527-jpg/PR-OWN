# 15 — 第一轮架构审查修订说明（Revision Notes）

> 回应对象：`clipboard-20260813-231742.850361-000006.md`（第一轮审查报告）。
> 状态：P0-1~P0-6、P1-1~P1-10 已全部修订；版本安排与过度设计收敛已并入对应文档。
> 约定：每条给出"结论 → 修订位置 → 新契约摘要"。本文件不重复全文，只做定位与裁决。

---

## 0. 修订总览

| 类别 | 数量 | 状态 |
|------|------|------|
| P0 严重问题 | 6 | ✅ 全部修订 |
| P1 关键缺失/不一致 | 10 | ✅ 全部修订 |
| 版本安排调整 | 3 组 | ✅ 并入 01/06/07/08/09/11/12/13/14 |
| 过度设计/表述收敛 | 4 项 | ✅ 并入 02/03/04/12 |

---

## 1. P0 严重问题回应

### P0-1：V2 Finding 聚合存在两个所有者 → 已定唯一所有权

**结论**：采纳。任何 Strategy（SinglePass/MultiRole/Agentic）只返回 `CandidateFinding + SourceRunResult`；`schema/location/evidence` 校验、聚类/去重/来源合并、Judge、`accepted/body_only/suppressed` 决策全部由 `ReviewService` 编排的统一 `FindingPipeline` 执行。Judge 是统一 Pipeline 的可插拔阶段，仅 V2/V3 启用。

**修订位置**：`03` §4 图、§5 所有权契约、§6 组件职责；`04` §3 时序与 barrier；`07` §5 唯一所有者声明、§6 Judge。

### P0-2：SQLite 与 GitHub 发布被描述为可回滚单事务 → 已改 Saga/Outbox

**结论**：采纳。明确"SQLite 事务无法回滚已成功的 GitHub API 副作用"。发布走 Saga 状态机：`DB 保存 plan(prepared) → 逐条发布记录 remote_comment_id → 必要评论未全成功则 plan=partial 且不推进 watermark、不清理旧评论 → 全部成功 plan=published → 清理 superseded 旧评论 → 最后推进 watermark`。幂等实现为 DB 映射 + marker + 查询 update/create 组合，不假设 GitHub API 原生幂等键。

**修订位置**：`07` §7；`10` §7；`05` §2 PublishPlan/CommentPlan、§6 SQLite publish_plans/published_comments；`04` §3 watermark 行、§7/§8 降级表。

### P0-3：稳定身份与本次实例混用主键 → 已拆三重身份

**结论**：采纳。`finding_occurrence_id`（UUID，run 内主键）、`fingerprint`（内容哈希，跨 run/跨角色匹配，反馈与幂等关联）、`cluster_id`（本次聚类逻辑问题）。SQLite 以 occurrence 为主键，`UNIQUE(run_id, fingerprint)`；反馈与发布记录关联 fingerprint。

**修订位置**：`05` §3 三重身份、§6 SQLite；`11` §1 ID 体系；`06` §7 证据索引；`04` §7 幂等表述。

### P0-4：模型位置字段契约自相矛盾 → 已分 claimed/canonical

**结论**：采纳。模型提供位置**线索**（`claimed_path/claimed_start_line/claimed_end_line` 或 `chunk_id/evidence_ref`）；程序确认/重定位为 **canonical 事实**（`canonical_path/canonical_start_line/canonical_end_line`），无法重定位时降级 body_only/suppressed。模型无权写 canonical 字段与 verified 状态，但**可以**提供位置线索（不再是"忽略并覆盖"）。

**修订位置**：`05` §3 字段表、§4 幻觉防线；`07` §5 location 校验；`10` §6 验证链。

### P0-5：V1 审查粒度与并发/失败语义冲突 → 已定 per-file map-reduce 契约

**结论**：采纳。V1 固定为：按 changed file 建 ReviewTask（map）→ 文件内按 hunk/符号构造 chunk → 每文件通常一次模型审查（超预算才分块）→ 文件任务间 model semaphore 限并发 → 汇总后生成摘要（reduce）。"单文件失败→PARTIAL"与 `file_tasks=3/model_requests=3` 因此有真实语义。

**修订位置**：`04` §2 契约与时序图、并发模型、错误表；`03` §5 实现表；`12` V1-b/V1-d 任务卡。

### P0-6：grace round 与硬预算冲突 → 已引入 reserved finalize budget

**结论**：采纳。总硬预算内拆分 `exploration_budget` 与 `reserved_finalize_budget`（预留 10%）：探索额度耗尽才进入 finalize/grace，finalize 只能消耗预留额度，**任何路径不突破 total_hard_budget**。墙钟硬超时与外部取消不再调用模型；网络超时走有限 retry policy，不借用 grace 概念。

**修订位置**：`08` §5 预算体系、§4 loop 伪代码（ModelTimeout 分支）、§5 关键分支表；`10` §7 硬预算；`09` §6 YAML（agent 预算）。

---

## 2. P1 关键缺失/不一致回应

### P1-1：dry-run"不触碰 GitHub"表述错误 → 已修正

**结论**：采纳。dry-run = 不执行任何 GitHub **写操作**，但仍可能**读**取（`--pr` 模式）；本地 `--base/--head` 模式可完全离线。

**修订位置**：`01` §2 场景 1。

### P1-2：V3 不应简单用按需检索替换 L3 → 已改为"最小确定性 L3 + 工具增量探索"

**结论**：采纳。V3 保留 V2 的确定性最小 L3（修改符号定义、直接 import、相关测试）作为初始上下文，再允许 Agent 增量探索，避免工具轮数浪费与成本退化。

**修订位置**：`06` §1 版本差异、§8 版本对照表；`11` §8 V3 验收；`12` V3-c。

### P1-3：裁剪 diff 策略不够安全 → 已改"当前任务 diff 不裁 + 被裁 hunk 显式处置"

**结论**：采纳。裁剪顺序：先裁低优先 L4 → 再裁 L3 → 才裁**非当前任务**的 L2 diff；被裁 hunk 必须创建独立后续任务或标记未覆盖（CoverageManifest reason=truncated），不得静默舍弃。L0 与输出空间不可裁。

**修订位置**：`06` §2 裁剪顺序；`07` §8 覆盖披露。

### P1-4：AgentTask 状态机缺 WAITING_TOOL 异常出口 → 已补全

**结论**：采纳。新增 `WAITING_TOOL → RUNNING(error observation)`、`WAITING_TOOL → FAILED`（不可恢复错误）、`WAITING_TOOL → CANCELLED`（等待期取消）。ReviewRun 图同步补 `PENDING → FAILED/CANCELLED` 边，与转换表一致。

**修订位置**：`04` §5 两个状态机图与非法转换说明。

### P1-5：无效工具调用不计预算容易被刷 → 已计入 attempt/轮数预算

**结论**：采纳。非法/无效调用计入 `attempt_count` 与轮数预算；只有成功执行计入 `successful_tool_calls`。`ToolCall` 模型新增 `attempt_count`。

**修订位置**：`08` §3 校验、§5 预算表；`05` §2 ToolCall 字段。

### P1-6：不应保存 thinking_summary → 已改 decision_summary

**结论**：采纳。无工具动作时只保存结构化 `decision_summary`（决策摘要），不要求、不持久化隐含思维链；与"不保存 raw CoT"全文档一致。

**修订位置**：`08` §4 loop 伪代码、§6 事件循环；`04` §4 事件循环伪代码。

### P1-7："疑似注入"默认当 Finding 制造噪声 → 已改为安全遥测事件

**结论**：采纳。疑似注入默认记录为 observability 遥测事件，不进评论流；仅当构成实际可利用的数据流漏洞时才由审查角色生成代码 Finding（可配置升级）。

**修订位置**：`09` §7。

### P1-8：本地仓库并非天然可信 → 已按"来源与 ref"分类

**结论**：采纳。判定原则：系统配置/默认分支受信任规则可信（哈希校验）；任何待审代码（GitHub PR 或本地 checkout 的待审分支）同权，均为不可信数据。本地/远程不构成信任依据。

**修订位置**：`02` §4 信任表；`10` §2 信任边界。

### P1-9：数据库关系与类型错误 → 已修正

**结论**：采纳，逐项：
- `PublishPlan ||--o{ Finding`（方向反转）。
- `Finding.sources: list[FindingSource]`（多来源）。
- Token 计数字段为 `int`；`cost_usd` 为 `float`。
- `is_outside_diff` 为 bool。
- CoverageManifest 改为 `items: list[CoverageItem{target, reason, stage, detail}]`。
- `ToolCall`（调用状态）与 `ToolResult`（返回数据/错误）分离建模。

**修订位置**：`05` §1 ER、§2（CoverageItem/PublishPlan/ToolCall/ToolResult/ModelUsage）、§6 SQLite。

### P1-10：幂等需说明 GitHub 真实实现 → 已明确三机制组合

**结论**：采纳。GitHub 评论 API 不保证原生幂等键，采用：隐藏 HTML marker（`<!-- reposage:plan_id:comment_id -->`）+ DB `remote_comment_id` 映射 + 查询现有评论后 update/create 补偿，三者组合保证重复运行不刷屏。

**修订位置**：`10` §7；`07` §7；`05` §2 CommentPlan.marker/remote_comment_id。

---

## 3. 版本安排调整

| 版本 | 调整 | 修订位置 |
|------|------|----------|
| V1 | per-file map-reduce 契约落地；occurrence/fingerprint 拆分与 Saga 发布基础从 V1 建好；`file_tasks` 与真实执行模型对应 | `04` §2、`05` §3/§6、`11` §8、`12` §2 |
| V2 | Judge 仅 keep/downrank；`needs_evidence` 为内部标记（V3 才触发 Agent 补证）；基础 Python 审查规则属 V1，反馈记忆属 V2 | `07` §6、`06` §1 L4 版本列、`01` FR-19 |
| V3 | 保留确定性最小 L3；tool calling 方案不在文档预设默认，同一评测集对比 A/B，JSON 不足允许 V3 暂缓 | `06` §1、`08` §9、`14` OQ-11、`12` V3-d |

## 4. 过度设计/表述收敛

| 项 | 收敛 | 修订位置 |
|----|------|----------|
| 50–200 行承诺 | 删除具体行数，改"以测试复杂度与状态数量评估工作量" | `03` §7 |
| 里程碑顺序 | V1 先本地 fixture/dry-run，再接远程 PR 与发布 | `12` §2 V1-a/V1-e |
| C4 命名 | 标注为逻辑组件图；真正 Container = CLI/Action runner、RepoSage 进程、SQLite、GitHub、LLM API | `02` §1、`03` §3 |
| 边界流程 | 补 is_draft、空 diff、全过滤、纯删除、fork 权限不足、审查中断恢复 | `04` §7b |

## 5. 通过标准自查（对照审查报告 §7）

- [x] 每项运行职责只有一个所有者（P0-1：统一 FindingPipeline）。
- [x] 不再声称本地 DB 回滚 GitHub 已完成操作（P0-2：Saga/Outbox）。
- [x] 同一 Finding 可跨 run 匹配且保留每次运行历史（P0-3：occurrence/fingerprint/cluster）。
- [x] Candidate 定位线索与程序确认事实分离（P0-4：claimed/canonical）。
- [x] V1 调用粒度、并发、错误与成本可从文档直接实现（P0-5：per-file map-reduce）。
- [x] 硬预算在所有路径上不被 grace/retry 突破（P0-6：reserved finalize budget）。
- [x] 状态机覆盖等待期间的失败与取消（P1-4：WAITING_TOOL 异常出口）。
- [x] 文档中模型、数据库、配置字段一致（P1-9 全项 + 全文一致性检查）。

## 6. 遗留事项

- OQ-1（DP-V4-PRO 实测）与 OQ-11（tool calling 同集对比）仍是 V1 前唯一阻塞项；其余开放问题保持安全可逆默认。
- 本文档随 `00` 文档地图纳入阅读顺序；冻结架构后并入 ADR 记录。
