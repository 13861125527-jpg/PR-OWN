# 16 — 第二轮审查修订说明（Revision Notes Round 2）

> 回应对象：`clipboard-20260813-233230.382051-000007.md`（第一轮回应复审报告）。
> 结论：主体架构"有条件通过"；本轮完成复审报告第 3 节的定点修订，可冻结 `architecture-v1` 进入 Phase 0。
> 本文件只记录二轮改动；一轮改动见 `15-revision-notes.md`。

---

## 0. 修订总览

| 编号 | 主题 | 状态 |
|------|------|------|
| P0-R2-1 | fingerprint 两层键 + 聚类先序 + 反馈条件匹配 | ✅ |
| P0-R2-2 | Saga 必要评论定义 + cleanup_pending + supersede 独立任务 | ✅ |
| P0-R2-3 | 费用预算改为 admission control（保守估算） | ✅ |
| P1-R2-1 | MultiRolePipeline 改名 MultiRoleReviewer | ✅ |
| P1-R2-2 | 重复段落删除、L4 版本修正、occurrence 字段名统一 | ✅ |
| P1-R2-3 | ChangeRequest 抽象（本地/远程统一） | ✅ |
| P1-R2-4 | 部分成功归并（required/optional、分析/发布分离） | ✅ |

## 1. P0-R2 修订回应

### P0-R2-1：fingerprint 两层键

**采纳**。明确两层键：

```text
fingerprint = hash(repo, head_sha, canonical_path, line_anchor,
                   normalized_category, normalized_rule_or_issue_key)
cross_run_match_key = hash(repo, canonical_path, symbol_or_anchor,
                   normalized_category, normalized_rule_or_issue_key)
```

- **聚类先于 fingerprint**：聚类按同文件/重叠邻近行/兼容类别/语义相似规则合并候选，再为聚类结果计算指纹；不先依赖指纹做语义去重。
- 自然语言字段（title/explanation）不进指纹。
- **最终字段命名（用户裁定）**：run 内去重统一为 `fingerprint`，跨 run 匹配统一为 `cross_run_match_key`；不保留 `candidate_fingerprint` 字段名。
- 反馈记忆支持条件化匹配（scope/path/symbol/category/rule_key/pattern），不只 fingerprint 精确匹配，代码移动后仍可命中。

**修订位置**：`05` §3 指纹语义、§2 FeedbackMemory、§6 SQLite（findings 加 cross_run_match_key，feedback 表扩展条件字段）；`06` §5 知识治理。

### P0-R2-2：Saga 必要评论与终态

**采纳**。固定必要评论定义：
- **必须项**：summary comment；V1 中全部 accepted 的 inline/body 评论（配置可放宽）。
- **非必须项**：telemetry/coverage 附件；supersede 清理。

supersede 清理拆为独立 `PublishOperation`（可重试）：核心新结果成功后即可推进 watermark，清理未完成时 plan=`cleanup_pending`，完成后 `completed`——不存在 `published` 后无终态的问题。

**修订位置**：`07` §7（必要评论 + Saga 步骤）；`05` §2 PublishPlan 状态枚举（+cleanup_pending/completed）、新增 PublishOperation、CommentPlan.required；`10` §7 watermark 同步。

### P0-R2-3：费用硬预算为 admission control

**采纳**。Token 维度是请求前可预留的硬上限；费用只能保守估算：
- 发送前按 `input_tokens + max_output_tokens` 做最坏费用预留，超预留拒绝请求（admission control）。
- retry 先预留再重试；拿不到价格/usage → 成本状态 `unknown/unverified`，不假装精确。
- 墙钟到期不再发新请求；已在途请求取消并忽略迟到结果。

**修订位置**：`10` §7 硬预算；`08` §5 预算表（max_cost_usd 语义改为"估算上限"）。

## 2. P1-R2 修订回应

| 编号 | 结论 | 修订位置 |
|------|------|----------|
| P1-R2-1 改名 | `MultiRolePipeline` → `MultiRoleReviewer`（避免与统一 FindingPipeline 混淆） | `00` §5、`03` §4/§5、`04` §3 |
| P1-R2-2 残留 | 删除 `07` §5 重复段落；`03` §8 演进表 L4 基础规则改属 V1（反馈记忆属 V2）；全文统一 `finding_occurrence_id`（`05`/`06`/`10`/`11`）；`finding_id` 不再作为领域字段（如作为用户可见别名须显式注明） | `07` §5、`03` §8、`05` §2/§3/§6、`06`/`10`/`11` |
| P1-R2-3 ChangeRequest | `PullRequest` → `ChangeRequest{source: local_range\|github_pr, external_id: optional, ...}`；远程字段（title/description/author/is_draft）optional；runs.external_ref 可空 | `05` §2、§1 ER、§6；`02` §5 契约（get_changes）；`04` §2 时序 |
| P1-R2-4 部分成功 | 必需任务失败/未覆盖 diff → PARTIAL；optional 任务失败 → COMPLETED+warnings；分析状态与 publish_status 分离；StageResult 加 required | `04` §5 状态机、§7；`05` §2 ReviewRun/StageResult；`11` §8 验收 |

## 3. 非阻塞建议的处理

| 建议 | 处理 |
|------|------|
| C4 图后续美化 | 不阻塞；已标注逻辑组件图（`02`/`03`） |
| Precision 门槛为假设 | 保持为设计初值，真实数据集校准（`11` §8 注明） |
| DP-V4-PRO 实测 | 保持开放问题 OQ-1/OQ-11 |
| GitHub Token 权限名 | 接入 Action 时按官方文档核对，架构阶段不写死 scope 名（`10` §5 已弱化具体名） |

## 4. 冻结条件自查

- [x] P0-R2-1~3 全部修订（指纹/聚类、Saga 终态、费用 admission control）。
- [x] P1-R2-1~4 全部修订（改名、残留清理、ChangeRequest、部分成功归并）。
- [x] 一致性检查：`finding_occurrence_id` 统一、无重复段落、围栏配对、交叉引用一致。
- [x] 15/16 两份修订说明并存且互不矛盾（15 记录一轮，16 记录二轮）。

**结论**：建议冻结 `architecture-v1`，随后进入 Phase 0（工程骨架、domain 模型、Provider 协议、Fake、基础评测）；其中 Finding/发布表结构以 `05` §6 为准，不再改动。
