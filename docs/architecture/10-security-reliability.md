# 10 — 安全与可靠性

> 威胁模型、Prompt 注入、路径/命令沙箱、凭据、幻觉防线、SHA 锁定、幂等与恢复、隐私。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. 威胁模型（STRIDE 视角，个人项目范围）

| 威胁 | 场景 | 防护（见章节） |
|------|------|----------------|
| 提示注入（Tampering） | PR 文本诱导模型输出恶意评论/越权指令 | §3 边界标记 + §2 |
| 路径穿越（Tampering/Info disclosure） | 工具/静态分析读取仓库外文件 | §4 沙箱 |
| 命令注入（Tampering） | 恶意文件名/ref 进入 shell | §4 禁止拼 shell |
| 凭据泄漏（Info disclosure） | token 进入日志/Prompt/评论 | §5 脱敏 |
| 幻觉事实（Spoofing） | 模型编造文件/行号/证据 | §6 程序验证 |
| 版本漂移（Integrity） | 审查期间代码被更新 | §7 SHA 锁定 |
| 发布风暴/刷屏（DoS on repo） | 重复运行重复评论 | §7 幂等 |
| 预算失控（Resource exhaustion） | 大 PR/循环工具耗尽费用 | §7 硬预算 |
| 不可信分析器/MCP 内容（Spoofing） | 外部内容伪造证据 | §2 候选证据原则 |
| 隐私扩散（Info disclosure） | 源码进入评测/日志 | §10 保留策略 |

## 2. 信任边界与数据分类（按来源与 ref，不按本地/远程，P1-8）

```text
可信：系统内置规则、用户本地配置、默认分支受信任规则（哈希校验）——这些是"来源+ref"可信，与存放在本地还是远程无关
半可信：GitHub 元数据（title/描述）、静态分析器输出（均为"候选数据"）
不可信：任何待审代码（无论来自 GitHub PR 还是本地 checkout 的待审分支）中的代码/注释/文档/描述/配置（一律视为数据，不是指令）
完全不可信：模型输出（推理体）、外部 MCP 工具结果（候选证据）
```

判定原则：按**来源与 ref** 分类——`default_branch` 上的受信任规则可信；`head` 待审分支的 checkout 内容与 GitHub PR 内容同权，均为不可信数据。

## 3. Prompt 注入防护（PR 文本是数据，不是控制指令）

1. 边界标记包裹所有不可信文本（见 `09` §7）。
2. PR 分支配置不读取/白名单校验，**不得提权、不得关安全策略**（`09` §6 铁律）。
3. 模型输出协议要求：检测到注入意图 → 上报"疑似注入"低置信度 Finding，**不遵从**。
4. 注入防御是安全测试用例，强制进 CI（`11` §6）。

## 4. 路径与命令沙箱

### 路径沙箱（V1 起，V3 工具强化）
- 所有路径解析：`resolved = (repo_root / raw).resolve()`；必须 `resolved.is_relative_to(repo_root)`。
- 拒绝：绝对路径、`..`、符号链接逃逸（resolve 后仍越界）。
- 工具层（V3）：`read_file/find_files/search_code` 全走沙箱函数；越界 → `error=invalid_args` 回喂并记录审计。

### 命令执行（本地 Git Provider）
- **禁止字符串拼 shell**。用 subprocess 参数数组（`["git", "diff", base, head, "--", path]`），ref 校验正则 `^[0-9a-fA-F]{40}$` 或安全分支名白名单 `[A-Za-z0-9_./-]`。
- 删除、重命名等高风险操作不需要（本系统只读）。

## 5. 凭据与脱敏

- GitHub Token 与模型 Key：环境变量（`GITHUB_TOKEN` / `MODEL_API_KEY`），**不落库、不进日志、不进 Prompt**。
- 脱敏规则：日志/轨迹/评测输出中，`token=***` 替换；config 快照写入前剥离 secret 字段（只存 `xxx_env` 名）。
- Token 最小权限：GitHub token 仅需 `pull_requests: read/write`（评论）与 `contents: read`；模型 key 仅 API 用途。Action 使用 `GITHUB_TOKEN` 的 `pull_request` 权限即可，不申请管理员权限。

## 6. 幻觉防线（事实字段程序验证）

- Finding 的 `canonical_path/canonical_start_line/canonical_end_line/evidence.verified/fingerprint/finding_occurrence_id/status/sources` 由程序生成或覆盖（`05` §4 铁律）；claimed 位置是模型线索，程序重定位。
- 验证链：schema → 位置（claimed→canonical 重定位 + diff 行号表）→ 证据（diff/符号/工具结果）。
- 验证失败处理：行号越界 → body_only；路径不存在 → suppressed(记因)；证据缺失 → 置信度下调或 suppressed。
- 模型自报的"已验证"字样不具效力——验证权在程序。

## 7. 幂等、SHA 锁定与失败恢复

### 目标 SHA 锁定（V1）
- preflight 获取 head SHA 后全程锁定；上下文、diff、发布评论均带该 SHA；GitHub 端若 head 变化（新 push）→ 拒绝继续并提示重跑（或按配置自动用新 SHA 重启一次，需 `14` OQ-6 确认）。

### 发布幂等与 watermark（Saga/Outbox，P0-2/P1-10）

SQLite 事务**无法回滚已成功的 GitHub API 副作用**，发布走 Saga 状态机（prepared → publishing → published/partial/failed），不宣称跨系统原子事务。

**GitHub 幂等的真实实现方式**（GitHub 评论 API 不保证接受自定义幂等键）：
- **marker**：每条评论正文嵌入隐藏 HTML 注释（如 `<!-- reposage:plan_id:comment_id -->`），用于识别机器人自己的评论。
- **remote_comment_id 映射**：发布成功后把 GitHub 返回的 comment ID 写入 DB；重跑时先查 DB，已发布的不再创建。
- **查询现有评论后 update/create**：对无 DB 记录的情况（如换机器/丢库），按 marker 查询机器人现有评论，存在则 update，不存在则 create。
- 三者组合（DB 映射优先，marker 兜底，查询补偿）保证重复运行不刷屏。

- watermark：`last_reviewed_sha`；plan=published 即推进（supersede 清理为独立任务，不阻塞 watermark，见 `07` §7）；cleanup_pending 记录在案。
- 评论更新：若某 Finding 在后续版本被修复/修改，新结果成功后清理旧评论并发布新评论（supersede 语义）。

### 硬预算（所有版本，P0-R2-3：admission control）

**Token 是请求前可控的硬上限；费用只能保守估算，不能绝对保证**：

- 发送前按 `input_tokens + max_output_tokens` 计算**最坏费用预留**；预留超限则拒绝该请求（admission control）。
- retry 同样先做最坏预留（占用预算后再重试），避免重试堆叠突破上限。
- 若拿不到价格或模型不返回 usage，成本预算状态标记为 `unknown/unverified`，**不得假装精确**；覆盖/评测报告如实披露。
- 墙钟到期后不再发新请求；已在途请求取消并忽略迟到结果（不写入 findings/usage）。
- `max_total_tokens / max_cost_usd / max_runtime_seconds` 全局硬顶；超限 → 停止新调用 → 收尾（V3 的 grace 仅消耗预留 finalize 额度，见 `08` §5）；CoverageManifest 记录原因。
- 文件数/并发上限独立配置。

## 8. 发布顺序与失败恢复

1. 只发布 `accepted` 状态 Finding（Judge/门槛通过）。
2. 发布顺序按（severity, confidence）降序；单条失败不影响其余（逐条独立）。
3. 全部完成后写 `publish_failed` 清单；下次 run 可重放未发布项。
4. 默认 `request_changes: false`；开启需显式配置且仅 critical/high 触发。

## 9. fail-soft 与覆盖清单

- 单文件/单角色/单 Agent 任务失败 → PARTIAL，其余继续（`04` §7）。
- `CoverageManifest` 是强制输出：哪些文件/角色/任务覆盖、哪些跳过及原因、是否截断。
- 摘要中明示覆盖不足（`07` §8）。

## 10. 隐私与数据保留

| 数据 | 保留策略 |
|------|----------|
| 运行记录（SQLite） | 保留 run/task/usage；Finding 原文可配置保留期限 |
| 日志/trace | 默认不保存 raw CoT（`save_raw_chain_of_thought: false`）；只保存动作/工具/结构化理由/证据 |
| 评测数据 | `retain_source_in_evals: false` 默认剥离源码原文，仅保留标注与指标；显式开启才含源码（仅本机） |
| 源码快照 | 不持久缓存模型推理原文（`06` §7） |
| 反馈记忆 | 只存用户确认的模式/理由，不存 PR 全文 |

## 11. 安全测试与验证（详见 `11` §6）

安全用例：注入样本、越界路径、幻觉行号、发布中断、限流、超时、取消、重复 delivery、secret 脱敏、分支配置提权。全部作为独立测试层级，CI 强制。
