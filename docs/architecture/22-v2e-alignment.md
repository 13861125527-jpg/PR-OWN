# V2-E 设计对齐（反馈记忆 + 增量 Watermark）

> 状态：V2-E **ACCEPTED**（Round 1：`docs/evidence/v2-e-implementation-review-round1.md`；P2 revoke 仓库边界已补）
> 对照：`docs/evidence/v2-e-status.md`、`docs/evidence/v2-e-compare.md`
> 前置：V2-A **ACCEPTED**；V2-B **ACCEPTED**；V2-C **ACCEPTED**；V2-D **ACCEPTED**
> 用户授权：V2-D Round 1 通过后进入 V2 最后阶段；V2-E 完成后进入 V3-A
> 契约来源：`01` FR-20/FR-21、`04` §3 增量审查、`05` §2 FeedbackMemory / §6 feedback 表、`06` §4–§6 条件化匹配与 L4、`07` §7 Saga watermark、`09` §6 配置、`10` §3 可信顺序、`11` §8 V2 DoD、`12` V2-e / ADR-008、`14` OQ-4、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §7 V2-E
> DoD 摘要：**条件化反馈记忆可标记/撤销/重放；禁止仅 repo 的全局压制；代码移动后仍可命中（不依赖行号锚 `cross_run_match_key`）；命中后重审不再报告、撤销后恢复；L4 注入用户确认反馈；增量审查是开关且默认关；watermark 仍只在 Saga 必要评论成功后推进；V1/V2-A/B/C/D 回归通过**

---

## 0. 一句话与边界

V2-E 交付两件产品，且必须分清「已经有的」和「还缺的」：

1. **反馈记忆**：把已存在但未接线的 `FeedbackMemory` + `feedback` 表做成可验收能力。入口本切片只做 CLI。匹配必须带 repo **以及**至少一项路径/符号/类别/规则/pattern/scope/key 条件。命中 `false_positive` / `wont_fix` 时由 **Pipeline 程序压制**（不指望模型自觉）；`rule` 只注入 L4、不压制。撤销是软删除；下一次审查即重放。
2. **增量 watermark 审查**：V1-E 已经会在 Saga 成功点写入 `publish_plans.committed_watermark`。本切片 **不改变 watermark 成功推进点**。缺口主要在 FETCH：默认仍审查 `base…head`；仅当 `review.incremental.enabled=true` 且该 PR 已有成功 watermark 时，改为审查 `watermark…head`。若增量 run 进入正式发布，还必须防止 supersede 清理误删未重审文件上的旧评论。

`ReviewStrategy.execute` 主签名不变。反馈匹配与增量 FETCH 都不是角色，不进 Role Registry，也不进 MultiRole。

**本里程碑禁止**：Agent / 工具循环、GitHub 评论指令（`/reposage-wontfix`）、embedding / 向量记忆、改 V2-C 融合（含 B2）、改 V2-D Judge 语义、改 `cross_run_match_key` 公式、默认打开增量审查、把 watermark 提前到 FETCH/REVIEW、重开 V2-A/B/C/D。

V2-D Round 1 遗留 P2（`judge_adjudicate` 日志 `n_keep=pending`）**不是**本里程碑任务，除非实现期顺手且不扩散 diff。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V2-E 用法 | 缺口 |
|---|---|---|---|
| 领域 | `FeedbackMemory`（位于 `domain/run.py`，含 `kind/scope/path/symbol/category/rule_key/pattern/cross_run_match_key/active/revoke()`）；`FeedbackKind`：`false_positive` / `wont_fix` / `rule` | 直接用；补 `id` | 无 `id`；无校验「禁止仅 repo」 |
| 枚举 | `ContextLayer.L4`、`ContextSourceKind.FEEDBACK`；`FindingVersion.actor` 已含 `user` | L4 来源与审计 | 未接线 |
| 存储 | SQLite `feedback` 表已在 SCHEMA 中（`id` PK）；`user_version=5` | CRUD + 软删除；**不必为反馈加列** | 无 `record_feedback` / `list` / `revoke` / `load_active` |
| 存储 | `publish_plans.committed_watermark`；Publisher 仅在必要评论成功后写入 | 增量 FETCH 只读最新成功 watermark | 无 `load_committed_watermark(pr_identity)` |
| 流水线 | `FindingPipeline.process`：merge → needs_evidence/Judge → 置信度门槛 | **唯一生命周期所有者**；反馈压制插在 Judge 之前、门槛之前 | 不读 feedback；`ACCEPTED` 不能转 `SUPPRESSED`（所以必须在 ACCEPTED 之前压制） |
| 上下文 | `ContextAssembler` L4 目前只有内置规则 | 在剩余 L4 预算内追加 active 反馈；有文件条件的必须文件命中 | 无 feedback 参数；L4 文本不得拼不可信路径 |
| 编排 | `ReviewService` FETCH：`get_diff(base, head)` | 增量开关打开且有 watermark 时改为 `get_diff(watermark, head)` | 始终全量 PR diff；`run.base_sha` 仍应是 PR base |
| 发布 | `Publisher._pr_identity` + Saga；dry_run / partial **不**写 `committed_watermark` | **不改 watermark 推进点**；增量 run 需禁用 supersede 删除或后置生产发布 | 本地 `local_range` 无 `external_ref` 时 identity 退化为 `head_sha`，不能当增量键 |
| CLI | 仅 `reposage review` | 增加 `feedback mark/list/revoke` 子命令 | 无反馈入口；OQ-4 / ADR-008 仍待定 |
| 配置 | `review.static` / `review.judge` | 新增 `review.feedback`、`review.incremental` | 无 |
| 评测 | `v2a/b/c/d_compare` | 钉死增量关、反馈匹配关（或空表） | 无 mark→重审、撤销恢复、代码移动、增量文件集对照 |
| 指纹 | `cross_run_match_key` 的 symbol 锚仍是行号（V2-D DP-14） | **不改公式**；代码移动命中靠 path/category/rule_key/pattern，不靠该键 | 若 `--from-finding` 默认抄 cross_run 键，行号一变就失配 |

**明确不复用为 V2-E 生产路径**：Agent `tool_loop`、GitHub blob API、GitHub 评论指令、用 Judge 代替反馈压制、第二张 watermark 表、把 `execute` 改成带 feedback。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-20 / FR-21 / `06` §4 / `11` §8 V2 / 交接 V2-E）

1. **可标记**：用户能把误报 / wont-fix / 强化规则写成带条件的仓库记忆。
2. **禁止全局压制**：仅 `repo`、其余条件全空 → 配置/CLI 错误，拒绝写入。
3. **程序压制**：命中 `false_positive` / `wont_fix` 的 Finding 在 Pipeline 内转为 `SUPPRESSED`（`MERGED` 或 location 阶段已经形成的 `BODY_ONLY`），不出现在行内/正文评论。
4. **L4 注入**：对正在审查的文件，把匹配的 **active** 反馈注入 L4（内置规则优先占预算）。不替代程序压制。
5. **可撤销 / 可重放**：`revoke` 只软删除；`list --include-revoked` 可审计；撤销后下一次审查恢复报告。无单独 `replay` 命令。
6. **代码移动仍可命中**：同一 path + category（及可选 rule_key）在行号变化后仍匹配。不把 V2-D 的行号锚 `cross_run_match_key` 当默认匹配键。
7. **增量审查可选**：默认全量 `base…head`。打开后只审查上次成功 watermark → 当前 head。无 watermark / 无稳定 PR 身份 / diff 失败 → 回退全量并 warning。
8. **Watermark 推进不变**：只在 Saga 达到既定成功点后写入；dry_run / partial / failed 不推进。
9. **对照**：mark 后抑制、revoke 后恢复、代码移动命中、增量开关文件集；V2-A/B/C/D compare 行为不变。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| Agent / 只读工具 / 补证 | V3 | V2 最后阶段仍是固定管道 |
| GitHub `/reposage-wontfix` 评论指令 | 后置（鉴权） | OQ-4：谁评论可信未解决；本切片 CLI 已能验收 FR-21 |
| embedding / 向量记忆 | `06` §6 | 结构化匹配足够；无启用信号 |
| 改 `cross_run_match_key` 公式 | — | 发布幂等锚；V2-D DP-14 冻结 |
| 改 V2-C 融合 / B2、V2-D Judge | — | 不重开 |
| 改 `execute` 主签名 | — | 与 V2-A/B/C/D 同一铁律 |
| 重写 Publishing Saga | — | watermark 推进已正确；只允许最小改动保护增量 supersede 语义 |
| 默认 `incremental.enabled=true` | — | 静默漏文件风险 |
| 第二张 watermark / `ReviewRun.watermark` 列 | — | 查询现有 `publish_plans` 足够；FETCH stage detail 记录 from/to |
| 让模型「自己遵守」反馈而不做程序压制 | — | 与 Judge 同一原则：程序事实不交给模型 |
| 修 V2-D P2 日志 nit | 可选 | 非阻断，不进 V2-E DoD |

### 2.3 与架构原文的已知差异（必须显式）

| 架构原文 | V2-E 裁决 | 处理 |
|----------|-----------|------|
| 交接「accept / ignore / false-positive」 | 不新增枚举。映射到已有 `FeedbackKind`：ignore/false-positive → `false_positive`；accept/wont-fix → `wont_fix`；强化规则 → `rule` | DP-2 |
| `14` OQ-4 / ADR-008 CLI vs 评论指令 | **本切片只做 CLI**；评论指令延后 | DP-1（关闭 OQ-4 对本里程碑的阻塞） |
| `04`「只审查 last_reviewed_sha → head_sha」 | 作为 **开关**，默认仍全量 PR diff | DP-10 |
| `05` ReviewRun.`watermark` 字段 | 不新增 run 列；成功点仍写 `publish_plans.committed_watermark`；FETCH `detail` 记录增量 from/to | DP-12 |
| `06`「命中时注入 L4」 | L4 **和** Pipeline 压制都做；`rule` 只 L4 | DP-6 / DP-7 |
| `06`「不只依赖精确指纹，代码移动后仍可命中」 | 默认匹配 **不**要求 `cross_run_match_key`；该键可选且 AND | DP-5 |
| `11` V2「watermark/Saga 故障注入恢复」 | V1-E 已验收；本切片不重做 Saga 故障注入，只测增量 FETCH 读 watermark，并补增量不误删旧评论的发布语义测试 | 回归现有发布测试 |
| Publisher `pr_identity` 在无 `external_ref` 时用 `head_sha` | 该 identity 随 head 变，**不能**驱动增量；此时忽略增量开关并 warning | DP-11 |

---

## 3. 反馈记忆

### 3.1 入口（关闭 OQ-4 对本切片）

只加 CLI 子命令（Typer 子 app），不改 `review` 主命令语义：

```text
reposage feedback mark --kind false_positive|wont_fix|rule
    --repo <dir>                  # 与 review 相同：解析后的仓库根；记忆键见 §3.2
    (--path | --scope | --symbol | --category | --rule-key | --pattern | --cross-run-key)+
    [--rationale "..."]
    [--from-finding <occurrence_id>]   # 从 SQLite 抄条件，见下

reposage feedback list [--repo] [--include-revoked]
reposage feedback revoke --id <int>
```

`--from-finding`：按 `finding_occurrence_id` 读库，抄 `canonical_path` → `path`、`category`、若有 `rule_id` 则 `rule_key`。**默认不抄** `cross_run_match_key`。仅当显式 `--with-cross-run-key` 才附加（行号锚，代码移动会失配，须在帮助里写明）。若该 finding 没有 `canonical_path` 且用户也没显式给其它条件，则拒绝写入，避免意外创建仅 repo 记忆。

`mark` 始终 INSERT 新行（新 `id`），不做条件 upsert。重叠记忆允许；匹配取「任意一条压制类命中」（见 §3.4）。

`revoke`：按 `id` + `settings.project.name` 软删除（`active=0` + `revoked_at=now`）。已撤销再调（同 repo）→ 成功幂等（方便重放脚本）。ID 不存在或不属于当前 `project.name` → CLI 非零退出。**禁止 DELETE**。

`list` 默认只出 `active=1`；`--include-revoked` 含历史。这就是审计/重放视图。CLI 输出默认不打印完整 rationale，除非 `--verbose`，避免终端日志泄露用户输入的大段内容。

GitHub 评论指令、Action `pull_request_review_comment`：**非目标**。`14` OQ-4 / `12` ADR-008 在本对齐 **ACCEPTED 后**登记为：V2-E = CLI；评论指令另开里程碑。

### 3.2 记忆键 `repo`

与现网 Pipeline / Publisher 一致：`Settings.project.name`（默认 `RepoSage`）。

不在本切片解析 `git remote`。多仓库共用一个 SQLite 时，用户必须把各仓库 `project.name` 配成不同值——与今天 fingerprint / marker 同一约束。

CLI `--repo` 只用于定位仓库根与默认 DB 路径（`storage.path` 相对该根），**不是** `FeedbackMemory.repo` 字符串本身。CLI 必须加载该仓库的 Settings；`FeedbackMemory.repo` 写 `settings.project.name`。

### 3.3 写入校验（禁止全局压制）

`repo` 必填（来自 settings）。下列至少一项非空，否则 `ValueError` / CLI exit ≠ 0：

`scope`、`path`、`symbol`、`category`、`rule_key`、`pattern`、`cross_run_match_key`

其它：

- `kind` 必须是 `FeedbackKind`。
- `category` 若给，必须是 `FindingCategory` 的 value。
- `path` / `scope` 若给：统一转 posix 路径，禁止绝对路径、`..`、空 segment；`scope` 允许 glob 通配，但仍不能逃出仓库语义。
- `pattern` 若给：长度 ≤ 256，`re.compile` 成功，否则拒绝写入（防垃圾正则；不做超时引擎）。运行期匹配前对目标文本截断到固定上限（建议 4k 字符），避免复杂正则在长 explanation 上放大成本。
- `rationale`：截断存储上限 2000；注入 L4 时再截到 400，并去掉 C0 控制字符（保留 `\n`/`\t`）。

### 3.4 匹配（纯函数，AND，未给字段 = 通配）

输入：一条 `FeedbackMemory` + 一条 `Finding`。仅 `active=True` 参与审查期匹配。

| 条件字段 | 匹配规则 |
|----------|----------|
| `repo` | 必须等于 Pipeline `repo`（`project.name`） |
| `path` | 对 `canonical_path` 做 posix 归一（`\`→`/`，去前导 `./`）后 **精确相等** |
| `scope` | `fnmatch` 对归一后的 `canonical_path`（如 `src/**/*.py`） |
| `category` | 等于 `finding.category.value` |
| `rule_key` | 等于 `finding.rule_id`（`None` 不匹配任何 `rule_key`，包括 `"model"`） |
| `symbol` | `symbol` 是 `trigger_condition` 或 `title` 的子串（大小写敏感）。Finding 无独立 symbol 字段，不改 Finding 模型 |
| `pattern` | 在 `title + "\n" + trigger_condition + "\n" + explanation` 上 `search` |
| `cross_run_match_key` | 与 Finding 该字段 **精确相等**（可选 AND；默认 mark 不写入） |

`path` 与 `scope` 同时存在时仍为 AND。

无 `canonical_path` 的 Finding：`path`/`scope` 条件视为不命中（不能靠空路径变成通配）。

多条记忆命中：

- 任一条 `false_positive` 或 `wont_fix` → 压制一次；`reason` 用 **id 最小** 的那条：`feedback:<id>:<kind>`。
- `rule` 不压制，只参与 L4。
- 同一 Finding 可同时被压制类命中（出局）和 `rule` 命中（L4 仍注入给该文件的后续审查；本 run 该 Finding 已出局不影响）。

### 3.5 何时压制（状态机）

`ACCEPTED → SUPPRESSED` **不在** `ALLOWED_TRANSITIONS` 中。因此反馈必须发生在置信度门槛 **之前**。

插入点（Pipeline 内，仍由 Pipeline 拥有生命周期）：

```text
cluster → fuse → merge
  → 反馈压制（merged 的 MERGED，以及 location 阶段的 BODY_ONLY）
  → needs_evidence / Judge（只处理仍为 MERGED 的）
  → 置信度门槛
```

放在 Judge **之前**：已标记误报的条目不再消耗 Judge 预算。

合法转换：

- `MERGED → SUPPRESSED`，`actor="user"`，`reason="feedback:<id>:<kind>"`
- `BODY_ONLY → SUPPRESSED`，同上（正文也不再报告）

已是 `SUPPRESSED` 的不改。canonical / evidence / fingerprint / `needs_evidence` **字节级不变**（只改 status + version）。

`rule` 命中：不 `record_transition`。

`review.feedback.enabled=false`：不匹配、不压制、不注入 L4；CLI `mark` 仍可写库。

默认 **`review.feedback.enabled=true`**：空表是空操作。与 Judge/static 不同——反馈只在用户显式 `mark` 后改变发布集合。A/B/C/D compare 仍钉 `false`，避免共用 DB 污染。

### 3.6 L4 注入

时机：CONTEXT，早于 Strategy。Service 按 `project.name` 加载 `active` 反馈，交给 `ContextAssembler`。

对每个 `ChangedFile`：用 **文件路径** 做第一层预过滤。L4 的目的是提示模型，不是事实压制；真正出局仍由 Pipeline 按 Finding 匹配。

- **文件级预过滤**：有 `path` 或 `scope` 的，必须先命中该文件路径才注入。
- 只有 category/rule_key/symbol/pattern、没有 path/scope 的：允许注入到本 run 审查文件，但必须全局排序并受 `max_feedback_items_per_file` 限制（建议默认 5），避免一条宽泛规则打满所有 L4。
- 只有 `cross_run_match_key`、没有其它条件的：**不注入 L4**，因为它无法在文件级判断且对代码移动不稳；仍可在 Pipeline 中作为精确 AND 条件匹配。
- 多条反馈候选时排序：压制类（false_positive/wont_fix）优先于 rule；有 path/scope 的优先于无 path/scope；再按 `id` 升序。预算不足按该顺序保留。

预算：先放内置规则（`06` 可信顺序：系统内置 > 用户反馈），剩余 L4 额度再放反馈。裁掉的反馈 → Coverage `TRUNCATED`，`detail` 带 `feedback:<id>`，不把 dropped 标 `COVERED`。

L4 文本（程序生成，进 system）：

- 只含 `kind`、`id`、可选 `category`/`rule_key`、截断后的 rationale。
- **不**把 `path`/`scope`/用户可控文件名拼进 L4（沿用 V1-d 内置规则的注入边界）。如果实现方认为需要解释作用范围，只写“applies to this file by stored path/scope condition”，不写原始路径字符串。
- rationale 视为用户确认、高于 PR 文本，**不**套 `[UNTRUSTED_CONTENT]`；但仍截断与剥控制字符。
- `source.kind=FEEDBACK`，`ref=feedback:<id>`。

`feedback.enabled=false`：零反馈 chunk。

### 3.7 领域补丁

`FeedbackMemory` 增加 `id: int | None = None`（对应表 PK）。`revoke()` 保持现行为。该模型当前在 `domain/run.py`，本切片不强制搬文件；若搬到新 domain 模块，必须保持导入兼容或同步更新所有引用。

SQLite：`feedback` 表已存在，**不**为反馈升 `user_version`。可在 SCHEMA / 初始化中补 `CREATE INDEX IF NOT EXISTS idx_feedback_repo_active ON feedback(repo, active)`，不算语义迁移；旧库初始化时也应执行该语句。

Storage 协议新增（async）：

- `record_feedback(memory) -> int`
- `list_feedback(repo: str | None, *, include_revoked: bool) -> list[FeedbackMemory]`
- `revoke_feedback(feedback_id: int, *, repo: str) -> RevokeFeedbackResult`（`revoked` / `already_revoked` / `not_found` / `wrong_repo`）
- `load_active_feedback(repo: str) -> list[FeedbackMemory]`
- `get_finding(finding_occurrence_id: str) -> Finding | None`（CLI `--from-finding`）
- `load_committed_watermark(pr_identity: str) -> str | None`（增量 FETCH）

---

## 4. 增量 Watermark 审查

### 4.1 推进（已有，冻结）

保持 V1-E：

- 仅正式 `mode=publish` 且必要评论成功后写 `committed_watermark = head_sha`。
- dry_run / partial / failed：**不写**。
- `cleanup_pending` 已推进 watermark（清理不阻塞增量）——现行为，不改。
- 不新增 watermark 表，不改 Saga 状态机，不改 fencing，不改变 `record_plan_published_with_cleanup()` 的成功推进点。

### 4.2 FETCH 增量（本切片唯一新增）

配置：

```yaml
review:
  incremental:
    enabled: false    # 默认关
```

`enabled=false`（默认）：`get_diff(req.base.sha, req.head.sha)`，与现网完全一致。

`enabled=true` 时：

1. 用与 Publisher 相同的 `_pr_identity(run)`（可抽纯函数，避免两处公式漂移）。
2. 若 `run.external_ref` 为空（本地 range 退化 identity）：**忽略增量**，全量 diff，warning `incremental_skipped:no_pr_identity`。
3. 否则 `load_committed_watermark(pr_identity)`：取该 identity 下最新一条 `committed_watermark IS NOT NULL` 且 `mode='publish'` 且 status ∈ `{published, cleanup_pending, completed}` 的 SHA；排序以 `rowid DESC` / 创建顺序为准，不看 dry-run plan。
4. 无 watermark（首次成功发布前）：全量 `base…head`。
5. 有 watermark：
   - `watermark == head`：仍走后续流水线；diff 为空则零 Finding；warning `incremental:no_new_commits`；**不**因空 diff 跳过 publish 摘要（现网空结果仍可发 summary）。
   - 否则 `get_diff(watermark, head)`。若 provider 失败（非祖先、force-push 等）：**回退** `get_diff(base, head)` + warning `incremental_fallback:diff_failed`。宁可重复审查，不可静默漏文件。

`run.base_sha` **永远**是 PR `req.base.sha`，不改写成 watermark。FETCH `StageResult.detail` 记录例如：`head=... incremental=0|1 from=... to=...`。

不把未出现在本段 diff 中的旧文件标成 skipped——它们不在本次变更集。

### 4.3 与发布幂等的关系

增量只缩小 **审查输入文件集**。发布仍用 `cross_run_match_key` + marker。

为避免误删未重审文件上的旧评论，`incremental.enabled=true` 时默认 **不执行 supersede 删除**，除非后续另有“本 run 覆盖了哪些旧 marker”的可靠证明。也就是说：增量模式可以新增/更新当前 diff 涉及问题，但不因为“本 run 没产出某旧 Finding”就删除旧评论。旧行内评论可能残留——这是增量语义，须在对照文档写明。默认关就是为了避免用户未预期的残留/漏报。

实现落点：可以给 `PublishPlan` 增加 `allow_supersede_cleanup: bool` 或等价字段，增量 run 置 False；但仍 **不改变 watermark 成功推进点**。若实现方不想改 Publisher，本切片也可先把增量限制为 dry-run/compare 评测能力，生产发布增量后置；二者必须在实现报告中二选一说明，不能静默用全量 supersede 语义。

---

## 5. 配置、编排、评测钉死

### 5.1 Settings

```yaml
review:
  feedback:
    enabled: true
    max_items_per_file: 5
  incremental:
    enabled: false
  judge:
    enabled: false
  static:
    enabled: false
```

`extra="forbid"`。不在此切片改 `09` 里 `symbol_retrieval` 的文档默认值（生产默认仍 True，与 V2-B 一致）。

### 5.2 Service

- PREFLIGHT：校验配置（无未知块即可；增量/反馈无 analyzer 名单）。
- CONTEXT 前：若 `feedback.enabled`，`load_active_feedback(project.name)` → Assembler；否则传空列表。
- PIPELINE：同一批 memories 传入 `process(..., feedback=...)`；空列表 = 空操作。
- FETCH：按 §4.2 选 diff 区间。
- PUBLISH：若本 run 实际使用增量 diff，必须禁用 supersede 删除，或明确将生产增量发布后置。
- **不**在 Service 里改 Finding status（生命周期仍只在 Pipeline）。

### 5.3 评测钉死

| 对照 | 必须钉 |
|------|--------|
| V2-A/B/C/D compare | `review.incremental.enabled=false`；`review.feedback.enabled=false`（不要只依赖空表） |
| V2-A | 继续钉 `symbol_retrieval=false`、`static.enabled=false`、`judge.enabled=false` |
| V2-B | 继续钉 static/judge off |
| V2-C | 继续钉 judge off |
| V2-D | 自身开关；新增钉反馈/增量 off |

不宣称真实模型因 L4 反馈而产生的质量收益，除非另跑 `real_compare`（非本里程碑 DoD）。

---

## 6. 明确 diff（实现期对照）

| 可改 | 不可改 |
|------|--------|
| `FeedbackMemory.id`；反馈匹配纯函数；Pipeline 插入压制；`PipelineMetrics` 增加 `feedback_suppressed` / `feedback_l4_injected` / `feedback_l4_dropped` 等计数 | `ReviewStrategy.execute` 主签名 |
| `ContextAssembler` 可选 feedback 参数与 L4 chunk | V2-C `_clusters_fuseable` / B2 |
| CLI 子命令 `feedback` | V2-D Judge keep/downrank 语义、`needs_evidence` 规则 |
| Settings `review.feedback` / `review.incremental` | `compute_cross_run_match_key` 公式 |
| Storage 反馈 CRUD + `load_committed_watermark` + `get_finding` | Publisher Saga / fencing / 何时写 `committed_watermark` |
| FETCH 选择 `get_diff` 的 from sha；FETCH detail | 默认 `incremental.enabled` |
| `v2e` 数据集 + compare；钉死 A/B/C/D | Agent、GitHub 评论指令、embedding |
| 抽 `_pr_identity` 纯函数供 FETCH 与 Publisher 共用；增量 run 禁用 supersede 删除或将生产增量发布后置 | 把 `run.base_sha` 改成 watermark |

允许的调用链变化：`process` 增加可选 `feedback=`（与已有 `adjudicator=` 同类）。禁止为反馈再包一层同步 `asyncio.run`。

---

## 7. 测试与对照

### 7.1 单测（纯函数优先）

- 仅 repo → 写入拒绝。
- path+category 命中；改 category 不命中。
- 行号变化、path/category/rule_key 不变 → 仍命中（代码移动）。
- 仅 `cross_run_match_key` 且行号变化 → **不**命中（证明不能靠该键做移动）。
- `rule` 不压制；`false_positive`/`wont_fix` 压制 `MERGED` 与 `BODY_ONLY`。
- 不把 `ACCEPTED` 再转 `SUPPRESSED`（插入点保证）。
- 不改 canonical/fingerprint。
- `revoke` 后 `active=False`，匹配函数跳过；库中行仍在。
- 非法 pattern / 超长 pattern 拒绝。
- `enabled=false` 零压制、零 L4 反馈 chunk。
- L4：内置规则优先；反馈超预算 → TRUNCATED 且非 COVERED；L4 文本不含文件路径。
- 仅 cross_run_key 的反馈不注入 L4；有 path/scope 的反馈只注入命中文件。
- 增量关：`get_diff` 参数为 base, head。
- 增量开 + 有 watermark：from=watermark。
- 增量开 + 无 watermark / 无 external_ref / diff 失败：全量 + 对应 warning。
- dry_run 成功不产生可被下次增量使用的 watermark（现网行为回归）。
- 增量生产发布不执行 supersede 删除；或生产发布增量明确后置并有测试证明不会误删旧评论。

### 7.2 对照评测 `v2e_compare`

主表不是 Fake LLM 的 P/R/F1：

| 表 | 内容 |
|----|------|
| 反馈抑制 | mark 后同一样本再跑：对应 Finding `SUPPRESSED`，不在 accepted/body 发布集 |
| 撤销重放 | revoke 后再跑：恢复为未标记时的状态（在 Fake 候选固定的前提下） |
| 代码移动 | 同行号偏移样本仍命中 path+category 记忆 |
| 全局拒绝 | 无条件 mark 失败 |
| 增量文件集 | 关：文件集 = 全量 PR；开：文件集 = watermark…head 触及的路径 |
| 开关隔离 | `enabled=false` 时 mark 存在也不压制 |

披露 Fake 限制。数据集建议 `reposage/evals/datasets/v2e_feedback.yaml`（小、可解释）。

---

## 8. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查；DP 默认生效 | 用户确认或默认 | — |
| T1 | 匹配/校验纯函数；`FeedbackMemory.id`；禁止全局；代码移动 vs cross_run 键 | 单测 | T0 |
| T2 | Storage CRUD + 软删除 + `get_finding` + `load_committed_watermark`；索引可选；**不升 schema 除非必要** | 存储测；旧库 v5 仍开 | T1 |
| T3 | Pipeline 插入压制；metrics；禁写事实；Judge 之前 | 状态机测 | T1 |
| T4 | Assembler L4 注入与预算/Coverage | L4 测 | T2 |
| T5 | Settings；Service 加载反馈 + FETCH 增量；抽 pr_identity；warning；增量 run 的 publish supersede 策略 | 开关测 | T2 T3 T4 |
| T6 | CLI `feedback mark/list/revoke`；`--from-finding` 默认不抄 cross_run | CLI 测 | T2 |
| T7 | `v2e` 数据集 + compare；钉死 A/B/C/D 的 feedback/incremental off | `docs/evidence/v2-e-compare.md` | T5 T6 |
| T8 | 全量回归、Ruff、mypy、`git diff --check` | CI | T7 |

**T0 完成前禁止改 Pipeline / Service / Publisher / CLI。**

目录（建议）：

```text
reposage/review/feedback.py          # 校验、匹配、apply 纯函数
reposage/evals/datasets/v2e_feedback.yaml
reposage/evals/v2e_compare.py
```

CLI 子命令可放在 `reposage/app/cli.py` 或 `reposage/app/feedback_cli.py`（避免把 review 命令文件撑爆）。不新增 `review/feedback/` 包，除非实现期明显超过单模块。

不改 `publishing/publisher.py` 的成功点推进逻辑，除非抽 `_pr_identity` 到无 IO 模块供 FETCH 复用（允许的小重构）。

---

## 9. DoD 与审查清单

### 9.1 交接 DoD

| # | 条目 | 落点 |
|---|------|------|
| 1 | accept/ignore/false-positive 类记忆（映射到现有 kind） | T1 T6 |
| 2 | 匹配含 repo/scope/path/symbol/category/rule/key，不能全局压制 | T1 |
| 3 | revoke / replay（软删除 + 下次审查）与可审计 list | T2 T6 T7 |
| 4 | 代码移动后仍可命中（不靠行号锚键） | T1 T7 |
| 5 | watermark 只在 Saga 成功点推进 | 回归 V1-E；本切片只读 |
| 6 | 增量 FETCH 默认关；打开后用 committed_watermark | T5 T7 |
| 7 | V1/V2-A/B/C/D 回归 | T8 |

### 9.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未引入 Agent / 工具 / GitHub 评论指令 / embedding
- [ ] 未改 V2-C 融合 / B2、V2-D Judge
- [ ] 未改 `cross_run_match_key` 公式
- [ ] 未重写 Publisher 成功点
- [ ] 默认 dry-run；不提交、不 push
- [ ] `incremental.enabled=false` 时 FETCH 与现网一致
- [ ] `feedback.enabled=false` 或空表时发布集合与 V2-D 相同
- [ ] 增量 run 不会因为未重审旧文件而 supersede 删除旧评论
- [ ] 日志无密钥、无整文件源码；L4 不含用户可控路径

### 9.3 实现期测试清单

- 全局 mark 拒绝 / 条件 AND / 通配未给字段
- mark → 抑制 → revoke → 恢复
- 代码移动命中；仅 cross_run 键不随移动
- `rule` 不抑制；L4 有注入
- BODY_ONLY 也可被反馈出局
- 增量关/开/无 watermark/无 PR identity/diff 失败回退
- dry_run 不推进 watermark
- A/B/C/D compare 钉死反馈与增量

---

## 10. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | V2-E 入口 = CLI only；GitHub 评论指令不做。关闭 OQ-4 对本里程碑的阻塞 | 评论鉴权未设计；CLI 足够验收 FR-21 |
| **DP-2** | 不新增 kind。ignore/FP → `false_positive`；accept/wont-fix → `wont_fix`；强化 → `rule` | 领域枚举已冻结；交接用词映射即可 |
| **DP-3** | `false_positive`/`wont_fix` 由 Pipeline **程序压制**；`rule` 只 L4 | 模型不可靠；与 Judge「程序拥有生命周期」一致 |
| **DP-4** | 压制在 Judge 之前、ACCEPTED 之前；actor=`user`，reason=`feedback:<id>:<kind>` | 状态机无 ACCEPTED→SUPPRESSED；省 Judge 预算；审计区分 confidence 压制 |
| **DP-5** | 未给出的条件字段为通配；全部空（除 repo）拒绝。代码移动靠 path/category/rule_key/pattern，不改 cross_run 公式。`--from-finding` 默认不抄该键 | 落实 P0-R2-1 且不破坏发布幂等 |
| **DP-6** | L4 注入 active 反馈；内置规则优先占 L4 预算；rationale 截断、不套 UNTRUSTED、不拼接 path | `06` 可信顺序 + V1-d 注入边界 |
| **DP-7** | `review.feedback.enabled` 默认 **True**（空表无操作）；compare 钉 False | 与 Judge 不同：无用户 mark 就不改发布集 |
| **DP-8** | mark = INSERT；revoke = 软删除；无 DELETE；无单独 replay 命令 | 可审计版本；下次 review 即重放 |
| **DP-9** | `FeedbackMemory.repo` = `settings.project.name`，不解析 git remote | 与现网 Pipeline/Publisher 同一键，避免两套身份 |
| **DP-10** | `review.incremental.enabled` 默认 **False** | 全量 PR diff 仍是安全默认；避免静默漏文件 |
| **DP-11** | 无 `external_ref` 时忽略增量（warning）；diff 失败回退全量 | 本地 identity 绑 head_sha；漏报比重复审查更糟 |
| **DP-12** | 不新增 run.watermark 列 / 第二张表；FETCH detail 记录 from/to；`run.base_sha` 保持 PR base | 成功点已在 `committed_watermark` |
| **DP-13** | 不改变 Publisher 的 watermark 成功推进点；增量 run 必须禁用 supersede 删除，或生产发布增量后置 | 防止增量未重审旧文件导致误删旧评论 |
| **DP-14** | 不改 `execute`；`process` 可增可选 `feedback=` | 与 adjudicator 同一扩展方式 |
| **DP-15** | SQLite `user_version` 保持 5，除非实现期确需新列 | `feedback` 表已存在 |
| **DP-16** | 本阶段不修 V2-D P2 日志 nit | 非阻断；避免里程碑混杂 |

若需推翻某条，只改本节与对应章节。

---

## 11. 冻结声明

设计冻结已解除（用户授权实现）。仍禁止：

1. 不引入 Agent / 工具 / GitHub 评论指令 / embedding / GitHub blob API。
2. 不改 `execute`，不改 V2-C 融合 / B2，不改 V2-D Judge，不改 `cross_run_match_key`。
3. 不把增量默认打开，不把 watermark 写到 FETCH，不恢复全局反馈压制。
4. 增量 run 不得因未重审旧文件而 supersede 删除旧评论。
5. 不提交、不 push、不打 tag（除非用户明确要求）。
