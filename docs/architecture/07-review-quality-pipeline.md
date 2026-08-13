# 07 — 审查质量流水线

> 角色体系、确定性门控、静态分析融合、聚合/去重/Judge、发布策略与覆盖披露。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. 审查优先级（内置规则，L0 承载）

```text
正确性/数据损坏 > 安全/权限 > 静默失败 > 并发/事务/资源 > 边界条件
> 测试缺口 > 性能 > 可维护性
```

- 低价值风格问题默认交给 lint（不在 Finding 中报告），保证输出聚焦。
- 优先级影响：severity 映射、Judge 排序、发布时是否进入行内评论。

## 2. 角色体系（RoleSpec）

### 角色清单与职责

| 角色 | 职责 | 输入 | 输出 | 启用条件（门控） | 版本 |
|------|------|------|------|------------------|------|
| `general` | 通用正确性、可读性、明显缺陷 | L0+L1+L2 | Finding 列表 | 总是 | V1 |
| `security` | 注入、鉴权、反序列化、密钥、权限 | L2+L3(敏感符号) | Finding | 新增行涉及 IO/鉴权/反序列化/加密/文件路径 | V2 |
| `silent-failure` | 异常被吞、回调失败、异步错误丢失 | L2 | Finding | 涉及 try/except、回调、async、fire-and-forget | V2 |
| `edge-case` | 边界输入、空值、类型假设 | L2 | Finding | 涉及输入解析、索引、比较、循环边界 | V2 |
| `concurrency` | 竞态、共享状态、事务、资源泄漏 | L2+L3 | Finding | 涉及共享变量/锁/连接/事务/线程 | V2 |
| `test` | 测试缺口与可测性 | L2+测试文件 | Finding | PR 含新逻辑但无对应测试变更 | V2 |
| `performance` | 明显复杂度/资源问题 | L2 | Finding | 涉及循环嵌套/大集合/IO 批处理 | optional |

**角色 = 一次模型调用（role prompt + 分块上下文），不是自主 Agent**。V3 的 Agent 是另一套执行单元，角色与 Agent 不混称（术语见附录 A）。只有被 Agent 选中做跨文件取证的角色子任务才是 Agent。

### 停止条件
- 门控不满足 → 不启用（不产生任何调用与成本）。
- 启用后输出为空 → 角色贡献 0 条，正常。
- 角色失败 → fail-soft，CoverageManifest.items 标记 reason=role_failed，不阻塞其他角色。

## 3. 门控规则（确定性、可复现）

门控 = 纯函数：`(ChangedFile 特征, 语言, 配置) → bool`。示例规则（正则/文件级判定）：

- `security`：新增行匹配 `open( / requests. / subprocess / eval( / pickle / jwt / sql / os.environ / path` 等敏感面，或文件为 `auth/*`、`*_view.py`（路由）类。
- `silent-failure`：新增行含 `except: / except Exception: pass / .add_done_callback / fire-and-forget / asyncio.create_task` 无 await。
- `concurrency`：含 `thread/async/with lock/transaction/Connection/` 共享对象。
- `test`：`ChangedFile` 无 `test_*` 且新增逻辑文件非测试。

> 门控规则属于 `prompts/rules/`，带版本与哈希；误触发由评测集校准（见 `11` §7）。

## 4. 静态分析融合（V2，1–2 个高价值分析器）

- 选型：ruff（规则子集，如 B 类 bugbear、S 类 bandit 安全规则）作为首发；理由：零配置、AST 级、规则可哈希、输出结构化。需实测其误报率决定启用子集。
- 融合方式：分析器输出 → 转换为统一 Finding（source=static_analyzer，verified=program）→ 进入同一去重/聚类/Judge 流水线。
- 与 LLM 结果重复时：来源合并（static 结果作为 evidence 加固 LLM Finding，或 LLM Finding 吸收 static 证据），不重复计数。
- 静态分析器**不可信内容**视为候选证据，路径/行号由程序校验（与模型同权）。

## 5. 聚合、去重、聚类与来源合并（统一 FindingPipeline）

**唯一所有者声明（P0-1）**：本流水线由 `ReviewService` 编排，是 Finding 正式生命周期的唯一所有者。任何 Strategy（SinglePass/MultiRole/Agentic）只返回 `CandidateFinding + SourceRunResult`；以下步骤全部在统一 Pipeline 中执行。

```text
所有候选（roles + static + agent）→ schema 校验 → location 校验 → evidence 校验
→ 聚类 → 去重 → 来源合并 → Judge(可插拔, V2/V3) → accepted / body_only / suppressed
```

| 步骤 | 算法（结构化） | 说明 |
|------|----------------|------|
| schema 校验 | 枚举白名单映射、字段类型 | 非法候选 → suppressed(记因) |
| location 校验 | claimed → canonical 重定位（见 `05` §3） | 越界 → body_only |
| evidence 校验 | 证据可追溯（diff/符号/工具结果） | 无法验证 → 降置信度或 suppressed |
| 聚类 | 键 = (file, 重叠行区间, category) | 同一问题跨角色归并 |
| 去重 | fingerprint（稳定问题指纹） | 相同指纹只留一条，合并来源与证据 |
| 来源合并 | 保留最高置信度来源 + 全部证据 | 不丢弃证据 |
| 排序 | severity × confidence × 优先级权重 | 决定发布顺序 |

**避免多角色重复报告的机制**：聚类键 + fingerprint 去重 + Judge 裁决；评测指标"重复率"监控（`11` §7）。

## 6. Judge（V2）——受限裁决器

- 输入：聚类后的候选 + 其证据。
- 输出：仅 `keep` / `downrank`。
- **禁止**：修改 canonical 位置/证据事实；禁止新增 Finding；禁止宣布"已验证"。
- `needs_evidence` 是**内部标记**，不是 Judge 输出：V2 中标记的候选降为 body_only 或 suppressed；V3 中才由 Agent 补证后重新进入流水线。
- 与模型不同：Judge 是**轻量裁决**（单次调用），不是审查角色。

## 7. 发布策略

### 行内 / 正文 / 仅摘要分类规则

| 类别 | 条件 | 呈现 |
|------|------|------|
| 行内评论 | `location_valid` 且严重度 ≥ medium 且置信度 ≥ 门限 | 锚定 diff 新增行 |
| 正文评论 | 位置越界/跨文件结论，但问题成立 | PR 正文分节列出 |
| 仅摘要 | 低严重度/汇总性/覆盖披露 | 只进摘要，不进评论流 |

### 幂等发布与 watermark（Saga/Outbox 状态机，P0-2）

SQLite 事务**无法回滚已成功的 GitHub API 副作用**，因此发布不宣称跨系统原子事务，改为 Saga/Outbox：

**必要评论定义（P0-R2-2）**：
- **必须项**：summary comment；V1 中所有 accepted 的 inline/body 评论（V1 全部视为必须项，配置可放宽）。
- **非必须项**：低优先 telemetry/coverage 附件；清理 supersede（独立任务，见下）。

```text
1. DB 保存 PublishPlan(status=prepared) 与每条 planned comment（标注 required/optional）
2. 逐条发布/更新新评论，每条记录 remote_comment_id 与状态（published/failed）
3. 若必要评论未全部成功：plan=partial，不推进 watermark，不清理旧评论；重跑恢复
4. 若全部必要评论成功：plan=published（optional 项失败仅记 warnings，不阻塞）
5. supersede 旧评论作为独立可重试清理任务（PublishOperation）：
   - 核心新结果成功后即可推进 watermark（旧评论删除权限问题不阻塞增量审查）
   - 清理未完成时 plan=cleanup_pending（而非 published 后又无终态）
6. 清理完成后 plan=completed，记录完成
```

- 幂等实现：本地 DB 的 `remote_comment_id` 映射 + 机器人 marker（隐藏 HTML 注释）+ 查询现有评论后 update/create 组合，不假设 GitHub API 原生支持自定义幂等键（见 `10` §7）。
- 重跑恢复：读取 DB 中 prepared/partial/cleanup_pending 的 plan 逐条恢复；已发布的经 remote_comment_id 复用，不重复创建。
- watermark：`last_reviewed_sha`；plan=published 即推进（supersede 清理任务可稍后完成，不无限阻止 watermark）；cleanup_pending 记录在案，重跑或后台任务补做。
- 默认 `request_changes: false`（只 comment）；开启时仅当存在 critical/high 且通过审批阈值，仍需用户显式配置（见 `14` OQ-3）。

## 8. 超大 PR 降级与覆盖披露

- 触发：文件数 > max_files 或预估 token > 预算。
- 动作：按（修改量 × 安全相关度）排序保留高价值文件 → 其余标记 skipped(原因) → 摘要明示"覆盖不足：N 个文件未审查，原因…"。
- 原则：**宁可披露不足，不可假装覆盖**；CoverageManifest 是强制输出项。
