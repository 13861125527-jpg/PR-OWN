# V1-e 设计对齐（Publishing + Saga/Outbox）

> 状态：设计对齐稿（待确认 3 个决策点后进入实现）
> 契约来源：`07` §7、`10` §7、`05` §2/§6、`12` §2 V1-e、`16` P0-R2-2
> DoD：**幂等验证通过；部分失败重跑恢复通过**（`12` §2；详细门槛 `11` §8）

## 1. 现状盘点（已就绪）

| 层 | 内容 | 状态 |
|---|---|---|
| domain | `PublishPlan` / `CommentPlan` / `PublishOperation` / `PublishCommentResult` / `ReviewRun.publish_status` | ✅ 完整 |
| domain | `PublishPlanStatus`(prepared/publishing/published/partial/failed/cleanup_pending/completed)、`CommentStatus`(prepared/published/failed/superseded)、`CommentKind`(inline/body/summary)、`PublishOperationKind/Status` | ✅ 完整 |
| config | `PublishingConfig`(dry_run/request_changes/idempotent/body_threshold_severity) + dry_run×request_changes 冲突校验 | ✅ 完整 |
| storage schema | `publish_plans` / `publish_operations` / `published_comments` 三表（含 marker/remote_comment_id/required/status） | ✅ 建表 |
| protocols | `GitProvider.publish_comments(plan) -> dict[comment_id, PublishCommentResult]` | ✅ 契约 |
| Fake | `FakeGitProvider.publish_comments`：幂等复用 remote_comment_id + 失败注入 | ✅ 实现 |
| service | `review()` 已到 pipeline 落库；`dry_run` 参数占位（`_ = dry_run`） | ⚠️ 未接 publish |

## 2. 缺口清单

1. **`publishing/` 包**（空）：Publisher 实现 Saga 状态机；
2. **storage**：publish 相关 CRUD（record_plan / record_comment_result / load_recoverable / update_status / watermark）+ `Storage` 协议扩展；
3. **service.py**：pipeline 之后接入 publish 阶段（dry-run 生成计划展示；`--publish` 执行 Saga）；
4. **摘要生成**：accepted findings → summary comment + inline/body 评论文本 + marker；
5. **watermark**：plan=published 推进 `last_reviewed_sha`；
6. **测试**：幂等（同 PR 二次运行零增量）、部分失败重跑恢复、cleanup_pending。

## 3. 契约要点（`07` §7 / `16` P0-R2-2）

- **必要评论**：summary 必须；V1 中所有 accepted inline/body 评论均为必须项（配置可放宽）。
- **Saga 状态机**：
  ```text
  DB 存 plan(prepared) → publishing
    → 逐条发布，每条记 remote_comment_id + status(published/failed)
    → 必要评论未全成功：plan=partial，不推进 watermark，不清理旧评论（重跑恢复）
    → 全成功：plan=published（optional 失败仅 warnings）
    → supersede 旧评论作为独立 PublishOperation（可重试）
       清理未完成：plan=cleanup_pending；完成：plan=completed
  ```
- **幂等三组合**（`10` §7）：DB remote_comment_id 映射优先 → 机器人 marker 兜底 → 查询现有后 update/create。
- **watermark**：`last_reviewed_sha`；plan=published 即推进，supersede 清理不阻塞 watermark。

## 4. 实现计划（分步）

1. **storage 扩展**：`record_publish_plan` / `record_comment_results` / `load_recoverable_plans` / `load_plan_comments` / `update_plan_status` / `record_operation` / `update_operation`；`Storage` 协议同步。
2. **publishing 包**：`Publisher`（构建 plan：分类 inline/body/summary、生成 marker、摘要文本）→ `publish(plan)`（Saga 循环 + 状态推进 + watermark）→ `recover()`（重跑恢复）。
3. **service 接入**：pipeline 后调 Publisher；dry_run 只生成不发布；正式模式执行 Saga；`run.publish_status` 分离落库。
4. **Fake 扩展**（若 supersede 纳入）：delete/update 评论能力。
5. **测试**：幂等、部分失败恢复、cleanup_pending、marker 复用。

## 5. 决策（已确认）

1. **supersede 清理**：纳入首轮，完整 Saga 到 completed——扩展 `GitProvider` 协议（delete/update comment）+ Fake 实现。
2. **watermark 落点**：`publish_plans.watermark`（schema 已有字段）。
3. **CLI**：先 service 层；`--publish` CLI 入口后续独立步骤。

## 6. 实现步骤（执行顺序）

1. 扩展 `Storage` 协议 + `SqliteStorage`：publish_plan / comment_result / operation / watermark / recoverable 加载；
2. 扩展 `GitProvider` 协议：`delete_comment`（supersede 清理）+ Fake 实现；
3. 实现 `publishing/publisher.py`：构建 plan（分类 + marker + 摘要）、Saga 发布、重跑恢复；
4. `service.py` 接入 publish 阶段（dry-run / 正式）；
5. 测试：幂等零增量、部分失败恢复、cleanup_pending、supersede、marker 复用。
