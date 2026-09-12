# V2-E 状态说明

> **V2-E 里程碑状态：ACCEPTED**
>
> 对齐稿：`docs/architecture/22-v2e-alignment.md`
> 实现验收：`docs/evidence/v2-e-implementation-review-round1.md`
> 对照报告：`docs/evidence/v2-e-compare.md` / `.json`

## 本轮实现

| 卡 | 落点 |
|---|---|
| T1 | `reposage/review/feedback.py`：禁止仅 repo、AND 匹配、代码移动不靠行号锚、`apply_feedback_suppressions` |
| T2 | Storage CRUD + 软删除 + `get_finding` + `load_committed_watermark`；`idx_feedback_repo_active`；`user_version` 仍为 5 |
| T3 | Pipeline 在 Judge 之前压制 MERGED/BODY_ONLY；actor=`user`；metrics.`feedback_suppressed` |
| T4 | L4 注入：内置规则优先、path/scope 预过滤、仅 cross_run 键不注入、条数上限、Coverage TRUNCATED |
| T5 | `review.feedback` 默认 True；`review.incremental` 默认 False；FETCH 读 watermark；增量 run `allow_supersede_cleanup=False` |
| T6 | CLI `reposage feedback mark/list/revoke`；`--from-finding` 默认不抄 cross_run 键；revoke 校验 `project.name`，缺失/错 repo 非零退出 |
| T7 | `v2e_feedback.yaml` + `v2e_compare`；A/B/C/D 钉死 feedback/incremental off |
| T8 | pytest / Ruff / mypy strict 全绿 |

## 对照（程序指标）

反馈抑制 passed=4/4（mark 出局、revoke 恢复、代码移动仍命中、enabled=false 不压制）；global_reject=True。不宣称真实模型质量收益。

增量：FETCH 在有 watermark 时只审 `watermark…head`；无 PR identity / diff 失败回退全量；dry_run 不推进 watermark；增量正式发布不 supersede 删除旧评论。

## Round 1 后补丁

P2：`revoke_feedback(id, *, repo) -> RevokeFeedbackResult`。同 repo 已撤销仍幂等成功；ID 不存在或不属于当前 `project.name` 时 CLI 非零退出。

## 明确非目标

- Agent / 工具 / GitHub 评论指令 / embedding
- 改 `cross_run_match_key` 公式、V2-C 融合 / B2、V2-D Judge
- 默认打开 `incremental.enabled`（仍为 False）
- 改变 Saga watermark 成功推进点
- V2-D P2 日志 nit

## 门禁

pytest / Ruff / mypy strict 全绿。未提交。下一阶段：V3-A 对齐稿 `docs/architecture/23-v3a-alignment.md`。
