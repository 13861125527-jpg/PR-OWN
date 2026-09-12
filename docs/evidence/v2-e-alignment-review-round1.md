# V2-E Alignment Review Round 1

日期：2026-08-16  
对象：`docs/architecture/22-v2e-alignment.md`  
结论：已直接修改对齐稿；当前状态为 **REVISED DRAFT / PENDING CONFIRMATION**。确认后可按 T1→T8 实现。

## 审查结论

V2-E 的方向成立：反馈记忆和增量 watermark 是 V2 的最后一段能力，但必须避免两个风险：

1. 反馈记忆不能变成“全局静默压制器”；
2. 增量审查不能因为未重审旧文件而误删旧评论。

本轮没有进入编码，只修改设计对齐稿。

## 已修改的关键点

### 1. 明确 V2-E 不改变 watermark 成功推进点

原稿强调“不重写 Publisher”，但 V2-E 的增量发布会影响 supersede 清理语义。现在改为更准确的边界：

- 不改变 `committed_watermark` 的成功推进点；
- 不改 Saga fencing；
- 但增量 run 如果进入正式发布，必须禁用 supersede 删除，或明确把生产增量发布后置。

原因：增量 run 只审 `watermark…head`，没有覆盖旧文件。如果继续用全量发布的 supersede 逻辑，可能把未重审文件上的旧评论误删。

### 2. 收紧反馈写入校验

已补充：

- `--from-finding` 如果没有 `canonical_path` 且用户没有显式给其它条件，必须拒绝写入；
- `path/scope` 必须转 posix，禁止绝对路径、`..` 和空 segment；
- `pattern` 写入时校验长度和可编译，运行期匹配前还要截断目标文本，避免复杂正则放大成本。

### 3. 明确 CLI 的 `--repo` 不是记忆 repo key

已补充：

- CLI `--repo` 只用于定位仓库根和默认 DB；
- `FeedbackMemory.repo` 必须写 `settings.project.name`；
- CLI 必须加载该仓库 Settings。

这避免 CLI 路径和 Pipeline/Publisher 的 repo 身份出现两套键。

### 4. 收紧 L4 注入规则

原稿允许没有 path/scope 的反馈注入所有文件，容易导致 L4 噪声。现在改为：

- 有 `path/scope` 的反馈，只能注入命中文件；
- 只有 category/rule_key/symbol/pattern 的反馈可以注入审查文件，但必须受 `max_feedback_items_per_file` 限制；
- 只有 `cross_run_match_key` 的反馈不注入 L4；
- L4 文本不打印原始 path/scope。

### 5. 补齐 Storage 与指标要求

已明确：

- `FeedbackMemory` 当前在 `domain/run.py`，只需补 `id`；
- `feedback` 表已存在，不因反馈 CRUD 升 `user_version`；
- 可补 `idx_feedback_repo_active` 索引；
- Storage 新增 `load_committed_watermark(pr_identity)`；
- PipelineMetrics 应增加 `feedback_suppressed` / `feedback_l4_injected` / `feedback_l4_dropped` 等计数。

### 6. A/B/C/D compare 必须显式关反馈与增量

已把“或保证空表”收紧为：

- `review.feedback.enabled=false`
- `review.incremental.enabled=false`

原因：反馈表是持久状态，不能依赖“当前空表”这种环境假设。

## 当前保留的设计决定

- V2-E 入口只做 CLI，不做 GitHub 评论指令。
- 不引入 Agent / 工具循环 / embedding / GitHub blob API。
- 不改 V2-C 融合。
- 不改 V2-D Judge 语义。
- 不改 `cross_run_match_key` 公式。
- `review.incremental.enabled=false` 默认关。
- `review.feedback.enabled=true` 默认开，但 A/B/C/D compare 显式关。
- `rule` 反馈只进 L4，不做程序压制。
- `false_positive` / `wont_fix` 由 Pipeline 程序压制。

## 给实现方的硬要求

1. 先做反馈匹配纯函数和校验，不要先改 Service。
2. 反馈压制必须发生在 Judge 之前、accepted 门槛之前。
3. `ACCEPTED -> SUPPRESSED` 不能出现。
4. 增量 FETCH 失败必须回退全量，不能静默漏文件。
5. 增量 run 不能用全量 supersede 语义误删旧评论。
6. V2-A/B/C/D compare 必须显式关闭 feedback/incremental。

## 最终判断

V2-E 对齐稿已从“方向可行但发布/反馈边界有风险”修改为“可交给实现方按任务卡执行”的状态。

建议下一步：用户确认后按 T1→T8 实现；不要在 V2-E 中加入评论指令、Agent、embedding、真实模型质量宣称或发布 Saga 重写。
