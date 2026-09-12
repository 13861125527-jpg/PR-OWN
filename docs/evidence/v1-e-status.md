# V1-e 状态说明（第六轮返工：租约 fencing 完整化，2026-08-15）

> **V1-e 里程碑状态：旧 Worker 全流程 fencing 隔离已闭环，待最终验收**
>
> 依赖 V1-d 基线（SHA `950b718`）。DoD：**幂等验证通过；部分失败重跑恢复通过**（`12` §2）。
> 第五轮修复了 claim 的 now 参数、租约释放、create-if-absent 与真实并发测试；第六轮按
> 最新版复验报告闭合了**唯一阻断：租约丢失后旧 Worker 未被完整停止**——带租约条件的
> UPDATE 不再静默失败，而是一致抛 `LeaseLostError`，Publisher 捕获后立即放弃本次发布，
> 不再产生任何本地状态写入或远端副作用。独立复验结论 **warn（一项 should-fix 已修复，
> 无 blocking）**，其中 `mark_plan_obsolete` 缺口也已在提交前闭合。

## 第六轮返工内容（对应最新复验报告 §3）

| 项 | 状态 |
|---|---|
| rowcount 校验 | `update_plan_status` / `finish_cleanup` / `record_plan_published_with_cleanup` 检查 `rowcount == 1`，0 行统一抛 `LeaseLostError` |
| LeaseLostError | `domain/run.py` 新增 `LeaseLostError(RuntimeError)`，作为租约丢失的统一信号 |
| published 事务原子性 | `record_plan_published_with_cleanup` 先 UPDATE plan（带 lease_owner），rowcount==0 则整个事务 rollback（禁止写入 operation）并抛异常 |
| Publisher 立即停止 | `publish()` 捕获 `LeaseLostError` 返回当前 DB 状态；`publish_comments` / `delete_comment` 远端调用前 `assert_lease`，丢失立即停止 |
| 附属写 fencing | `record_comment_results` / `record_operation` / `retire_remote_id` / `mark_comments_superseded` / `update_finding_status` 均加 lease_owner 并在锁内 `_assert_lease_locked` 校验，同一事务内校验+写入 |
| 陈旧 Worker 全流程测试 | `test_stale_worker_fully_fenced_after_takeover`：暂停 A → 租约过期 → B 接管完成 → 恢复 A，断言 A 不覆盖 B 状态、不产生额外远端副作用、旧 token 写状态抛 LeaseLostError |
| mark_plan_obsolete fencing | `_mark_plan_obsolete` 只废弃 `lease_owner IS NULL` 的 plan，不干扰持租约恢复中的 Worker（六轮复验 should-fix + 测试） |

## 验证结果

| 门禁 | 结果 |
|---|---|
| Pytest | **329 passed** |
| Ruff | All checks passed |
| mypy strict | Success: 45 source files |

新增测试（对应报告 §4/§6）：
1. `test_stale_worker_fully_fenced_after_takeover`：真正暂停 A（publish_gate）、租约过期、B 接管完成、恢复 A，断言 A 全流程被 fencing 隔离；
2. `test_expired_lease_takeover_fences_old_worker`：改造为断言旧 token 写状态抛 `LeaseLostError`（而非静默忽略）；
3. 前五轮并发测试全部保留：claim now 参数、partial 立即重试、cleanup 并发、publish 并发。

## 后续步骤（不作为本轮阻断）

1. Typer CLI 的 `--publish` / `--dry-run`；
2. 真实 GitHub Provider（`github_api`）实现 `publish_comments` / `delete_comment`（含读评论校验归属 + update-or-create）；
3. marker 兜底查询（真实 Provider 按 marker 查询现有评论）。

## 设计对齐文档

见 `docs/architecture/17-v1e-alignment.md`。
