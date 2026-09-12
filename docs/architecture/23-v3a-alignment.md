# V3-A 设计对齐（Tool Registry + Sandbox + Schema）

> 状态：**ACCEPTED**（实现验收：`docs/evidence/v3-a-implementation-review-round2.md`；对照见 `docs/evidence/v3-a-status.md`）
> 修订依据：`docs/evidence/v3-a-alignment-review-round1.md`（P1/P2 已合并进本文）
> 前置：V2-A/B/C/D/E **ACCEPTED**（V2-E：`docs/evidence/v2-e-implementation-review-round1.md`）
> 用户授权：V2-E 完成后进入 V3 第一阶段（本对齐稿）
> 契约来源：`01` FR-22、`03` §2 `tools/`、`04` §4 工具调用边、`05` §2 ToolDefinition/ToolCall/ToolResult、`06` §1 V3 增量探索、`08` §1–§3/§8–§9、`09` §6 `agent`、`10` §4 路径沙箱、`11` §6 安全组 / §8 V3、`12` V3-a / ADR-001/005、`14` OQ-1/OQ-11、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §8 V3-A
> DoD 摘要：**只读工具可注册、schema 校验、越界路径全拒；结果截断+错误协议；读文件/搜代码/查引用/读 diff 走锁定 head 快照；submit_finding/finish_review 只注册不执行；不实现 Agent loop / tool_loop / AgenticReviewer；默认 strategy 仍是 single_pass；V1/V2 回归通过**

---

## 0. 一句话与边界

V3-A 只交付 **工具层**：声明式 Registry、JSON Schema 参数校验、路径沙箱、超时与结果截断、统一错误协议。这是 V3-B Agent loop 的执行底座。本切片 **没有** 事件循环、没有 `llm.tool_loop`、没有 `AgenticReviewer`。

`ReviewStrategy.execute` 主签名不变。默认 `review.strategy=single_pass`。`agent.enabled` 保持 **False**。`review.strategy=agentic` 必须 **显式拒绝**（今天会静默落到 SinglePass，这是误导）。

**本里程碑禁止**：Agent loop / 状态机 / grace / 会话压缩、实现 `openai_compat.tool_loop`、方案 A/B 真实模型对照、MCP、把 ruff 再做成模型可调工具、改 V2-C 融合 / V2-D Judge / V2-E 反馈、改 `cross_run_match_key`、默认打开 Agent、复制一套审查主流程。

`08` §9 / `14` OQ-11「V3 启动前完成 tool calling 实测」**不是** V3-A 阻塞项，见 **DP-2**：那是 V3-B 门禁。V3-A 的 DoD 是 `12` V3-a「越界路径全拒」，不依赖模型。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V3-A 用法 | 缺口 |
|---|---|---|---|
| 领域 | `ToolDefinition` / `ToolCall` / `ToolResult` / `ToolCallStatus` / `ToolPermission`；`Evidence.tool_call_id`；`FindingSourceKind.TOOL_AGENT`；`EvidenceKind.TOOL_RESULT` | 直接用；`ToolCall` 补可选 `task_id` | 无 Registry；无执行信封；`ToolCall` 无 `task_id`（表有） |
| 领域 | `AgentMessage` / `AgentBudget`；`is_safe_repo_path` | 沙箱复用路径谓词；Budget/Session **不接线** | 无 `AgentSession` 类（留给 V3-B） |
| 枚举 | `ContextSourceKind` 无 `tool` | 结果 `source` 需要可追溯 | 补 `TOOL`（**DP-8**） |
| 协议 | `LLMProvider.tool_loop` 仍是前向声明；`openai_compat` 抛 `NotImplementedError` | **保持** | 不在本切片实现 |
| 协议 | `GitSnapshotProvider.get_blob` / `list_paths`；`as_snapshot_provider` | 工具读锁定 SHA，不读脏工作区 | 工具层未接 |
| 快照 | `HeadSnapshot` + `is_safe_repo_path`；LocalGit `git show sha:path` | `read_file` / `find_files` / `search_code` / `find_references` 的数据源 | L3 有 `max_files_read` cap；工具层要独立上限，避免一次搜索打爆 L3 cap |
| 符号 | V2-B `SymbolIndex` / `PythonAstExtractor` | `find_references` 复用，不另做索引器 | 工具调用需传入 snapshot + 可选 path 范围 |
| Diff | V1 parser + 行号映射 | `read_diff` 读**已解析**的本次 run diff，不再 `git diff` | 工具上下文要带 parsed files |
| 静态 | V2-C ruff → Candidate | **不**暴露为工具（**DP-3**） | 交接写了「运行已允许的静态检查」，本切片否决 |
| 存储 | `tool_calls` / `tool_results` 表已在 SCHEMA；`user_version=5` | 可选落库；**不升版本** | 无 Storage API；`tool_results` 无 `source` 列 |
| 编排 | `ReviewService`：`agentic` 落到 SinglePass | 显式拒绝 `strategy=agentic` | 静默降级会让人以为 Agent 已开 |
| 配置 | `Settings.agent`（enabled/预算/tool_protocol）已存在 | 只读；不改默认 | 无 `review.tools` 覆盖表 |
| 包 | `reposage/tools/__init__.py` 占位 | 按 `03` 填 registry/sandbox/各工具 | 空 |
| 评测 | v2a–v2e compare | 钉死 `agent.enabled=false`、strategy 非 agentic | 无沙箱对抗集 |

**明确不复用为 V3-A 生产路径**：`tool_loop`、MCP、把 Judge/反馈/ruff 当工具、读工作区未提交文件、`os.walk(repo_root)` 当 `find_files`。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-22 的本切片；`12` V3-a）

1. **可注册**：少量通用只读工具，`name` 唯一；定义含 description / JSON Schema / permission / result_limit / timeout。
2. **先校验再执行**：参数不过 schema → `invalid_args`，不进 handler。
3. **越界全拒**：绝对路径、`..`、盘符、UNC、resolve 后逃出 workspace、symlink escape → `invalid_args`；不抛未捕获异常杀死调用方。
4. **锁定 SHA**：文件类工具读 `head_sha` 快照（`GitSnapshotProvider`），不读脏 working tree。
5. **截断 + 错误协议**：`{status, data?, error?, truncated?, source?}`；截断对调用方可见。
6. **超时**：单工具 `timeout_s`；超时 → `timeout`，不泄漏半截内部栈。
7. **控制类工具占位**：`submit_finding` / `finish_review` 出现在 schema 清单里，执行返回稳定「未绑定」错误，留给 V3-B 接 Pipeline / 终局。
8. **可测**：Fake 快照 + 确定性对抗样本；不依赖真实 LLM。
9. **不破坏 V2**：默认审查路径零工具调用；V2 compare 行为不变。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| Agent loop / 状态机 / grace / 取消 / 重复终止 | V3-B | `12` V3-b |
| `llm.tool_loop` 方案 A/B 与 OQ-11 对照 | V3-B 门禁 | 见 DP-2 |
| `AgenticReviewer` / `strategy=agentic` 可跑 | V3-B | 本切片只拒绝误配 |
| 会话压缩 / evidence_index | V3-C | `12` V3-c |
| 跨文件 V2 vs V3 收益报告 | V3-D | 无 Agent 无法对照质量 |
| MCP / Skill 当工具 | `08` §8 optional | 降级扩展，不进 V3-A |
| `run_ruff` / 任意 subprocess 工具 | — | 静态分析已在 V2-C 管道；模型不得重跑并改写程序事实 |
| FTS / 向量 / 正则搜索 | `06` §6 / V3-B+ | V3-A `search_code` 只做字面量子串搜索，避免回溯型正则阻塞事件循环 |
| 改 `execute` 主签名 | — | 与 V2 同一铁律 |
| 默认 `agent.enabled=true` | — | 无 loop 时打开无意义且危险 |
| 新增 SQLite `user_version` | — | 表已在 |

### 2.3 与架构原文的已知差异（必须显式）

| 架构原文 | V3-A 裁决 | 处理 |
|----------|-----------|------|
| `08` §9「V3 启动前」完成 tool calling 实测 | 实测是 **V3-B** 门禁 | **DP-2**；本切片不调真实模型 |
| 交接 V3-A「运行已允许的静态检查」 | 不做 ruff 工具 | **DP-3**；V2-C 已融合 |
| `08` 用 jsonschema 库 | 用已有 Pydantic 做 args 模型 + `model_json_schema()` | **DP-5**；不新增依赖 |
| `03` 七个工具文件齐备 | 五件可执行 + 两件 stub | **DP-4** |
| `05` `tool_results.source` | 内存有；表无列 | 不升 schema；source 留内存 / 写入 data 信封（**DP-10**） |
| `04` 时序含 Agent loop | 本切片只实现 TOOLS 框 | 文档切片 |
| `11` §8 V3 全状态机 | 属 V3-B/C | V3-A 只收沙箱/schema 子集 |

---

## 3. 领域与执行信封

### 3.1 保持的模型

`ToolDefinition` / `ToolCall` / `ToolResult` 已在 `domain/models.py`，字段对齐 `05` §2。本切片最小增量：

- `ToolCall.task_id: str | None = None`（落库时必填，纯函数测试可空）。
- `ContextSourceKind.TOOL = "tool"`；`ToolResult.source.ref` 建议 `tool:<name>` 或 `tool_call:<id>`。

不在本切片落地 `AgentSession`。

### 3.2 执行上下文与统一快照协议

禁止让工具直接依赖 `HeadSnapshot | GitSnapshotProvider` 联合类型：两者的方法、同步/异步语义和缓存能力不同，handler 无法只按一个接口工作。V3-A 新增唯一工具快照协议：

```text
ToolSnapshot (Protocol)
  repository_id: str
  head_sha: str
  async list_paths() -> list[str]
  async get_blob(path: str) -> str | None
```

生产适配器由 `GitSnapshotProvider + locked head_sha` 构造；如需缓存，在适配器内部组合 `HeadSnapshot`，不得把 provider 作为隐藏参数再传给 handler。Fake 直接实现同一协议。五个只读工具只依赖 `ToolSnapshot`。

`list_paths()` 只返回锁定 SHA 中通过路径校验的条目；`get_blob(path)` 必须先确认 path 位于该路径集合。所有成功文件结果的 `source.sha` 必须等于 `ToolSnapshot.head_sha`。

```text
ToolWorkspace
  repo_root: Path          # resolve 后的仓库根（沙箱锚）
  snapshot: ToolSnapshot   # 内含唯一锁定 head_sha
  diff_files: 已解析的本次审查 diff（read_diff 用）
  symbol_index: SymbolIndex | None
  task_id / run_id: 可选，供审计
```

Workspace 由测试或（未来）V3-B 构造。V3-A **不**从 `ReviewService.review()` 自动调用工具。

### 3.3 统一信封

一次调用：

```text
id = new_tool_call_id()
validate name ∈ registry
validate args against tool schema     → 失败：status=invalid_args
sandbox any path-like args            → 失败：status=invalid_args
asyncio.wait_for(async_handler, timeout_s) → 超时：status=timeout
handler 按工具语义限额构造完整结构
envelope 按最终字符硬上限二次裁剪     → truncated=true
return (ToolCall, ToolResult)
```

错误协议（`08` §3）：`ok | error | invalid_args | timeout | cancelled`。未知工具 = `invalid_args`（或 `error=unknown_tool`，但 status 仍走枚举已有值：**DP-6** 用 `invalid_args` + error 文本 `unknown_tool`）。

Handler **禁止**抛到信封外，除非 `CancelledError`。内部异常 → `error` + 短类型名，不带路径拼接的用户可控长文。

取消是异常控制流，不是普通返回：`invoke()` 捕获 `CancelledError` 时可尽力将审计状态记为 `cancelled`，随后必须重新抛出；调用方不会收到常规 `(ToolCall, ToolResult)`。审计失败不得阻止取消传播，也不得把取消记成 `ok/timeout`。

`result_limit` 的整数保留为工具的主要语义数量上限；每个 handler 另受 `max_result_chars` 最终字符硬上限约束。handler 必须先裁剪列表/行/hunk 再序列化，禁止在 JSON 字符串中间截断。任一上限触发都设 `truncated=true`，输出采用稳定排序。V3-A 不宣称精确 token 上限。

### 3.4 重复检测（纯函数，可选）

`is_repeat(name, args, recent: list[ToolCall]) -> str | None` 可在 V3-A 提供，供单测。V3-A 信封默认 **不**自动拒绝重复（那是 loop 策略，V3-B）。若调用方传入 `repeat_of`，只记录。

---

## 4. 工具清单

权限一律 `ToolPermission.READ_ONLY`。无写、无 shell、无网络。

| name | V3-A | 数据源 | 主要参数 | 默认 limit / timeout |
|------|------|--------|----------|----------------------|
| `read_file` | 执行 | snapshot blob | `path` 必填；`start_line≥1`；`max_lines≤200` | 200 行 / 10s |
| `find_files` | 执行 | `list_paths(head_sha)` + glob | `pattern`（仓库相对 glob） | 100 条 / 10s |
| `search_code` | 执行 | 已列出路径的 blob（独立 cap） | `query` 字面量子串；可选 `scope` glob、`case_sensitive` | 50 命中 / 15s |
| `find_references` | 执行 | V2-B 抽取 | `symbol` 必填；`scope=repo\|file`；file 时要 `path` | 50 条 / 15s |
| `read_diff` | 执行 | 本 run 已解析 diff | `path` 必填 | 该文件 hunk / 10s |
| `submit_finding` | **stub** | — | 与 Candidate 对齐的最小 schema | 执行 → `error=not_bound` |
| `finish_review` | **stub** | — | `reason: str` | 执行 → `error=not_bound` |

`search_code.query`：非空、长度 ≤ 256，只做字面量子串匹配，不调用 `re.search`，不接受 regex 开关。路径按字典序稳定扫描；默认 `max_files_scanned=100`、`max_chars_per_file=4_000`、`max_total_chars_scanned=200_000`。命中条数或任一扫描上限触发均停止并令 `truncated=true`。正则、FTS、向量搜索后置，不进入 V3-A。

`find_files` / `search_code` **禁止** `os.walk` 工作区。只过滤 snapshot 路径集合。

`read_diff` 找不到该 path 的 diff → `ok` + 空 data + 明确说明，或 `error=not_found`（**DP-7**：`error=not_found`，避免模型把「无 diff」当成文件不存在）。

各工具结果上限：

| 工具 | 语义上限 | 最终硬上限 |
|---|---|---|
| `read_file` | 200 行 | 16,000 chars |
| `find_files` | 100 条 | 16,000 chars |
| `search_code` | 50 条；另受扫描上限约束 | 24,000 chars |
| `find_references` | 50 条 | 24,000 chars |
| `read_diff` | 20 hunks / 400 行 | 32,000 chars |
| 两件 stub | 无 data | error ≤ 256 chars |

### 4.1 不做的工具

- `run_ruff` / `run_analyzer`：程序事实已在 V2-C 进 Pipeline。
- 任意 `exec` / `git` 包装：LocalGit 已有安全 argv；工具层不再开命令面。
- MCP 代理。

---

## 5. 沙箱契约（`10` §4）

所有 path-like 参数走同一函数（建议 `reposage/tools/sandbox.py`）：

1. 字符串级：复用 `is_safe_repo_path`（拒空、绝对、`..`、盘符、UNC）。
2. 规范化：posix（`\` → `/`），去掉空 segment。
3. 若需要落盘核对（symlink）：`resolved = (repo_root / path).resolve()`，必须 `resolved.is_relative_to(repo_root.resolve())`。
4. Git blob 路径仍必须通过 1–2；`git show` 的 path 不得含 `:` 前缀技巧（LocalGit 已 `is_safe_repo_path`）。
5. 失败一律 `invalid_args`，错误文本不回显 `repo_root` 绝对路径（防信息泄漏）。

Windows：无创建 symlink 权限时，symlink 逃逸用例 `pytest.skip`。字符串级与 `resolve` 用例必须在 CI 跑。

`find_files` 的 glob 不得匹配到 `..`；`**` 只在已沙箱的路径列表上匹配。

---

## 6. Schema 与 Registry

### 6.1 定义

每个可执行工具一个 Pydantic args 模型；`ToolDefinition.parameters = Model.model_json_schema()`。Registry：

- `register(def, handler)`；`name` 重复 → 启动期 `ValueError`。
- `enabled()` / `schemas()`：给未来 `tool_loop` 用的 OpenAI-style 列表（`name/description/parameters`）。
- `get(name) -> ToolDefinition | None`。

内置七件在包导入或 `builtin_registry()` 一次性注册。测试可建空 Registry 再挂 Fake handler。

### 6.2 校验

`TypeAdapter(ArgsModel).validate_python(args)`（或 `model_validate`）。多余字段：Pydantic 默认忽略还是 forbid？**DP-9：`extra="forbid"`**，与 Settings 一致，防止模型夹带 `path2`。

不引入 `jsonschema` 包。

### 6.3 配置

不新增 `review.tools` 也能验收。若实现期要关某工具：`agent` 下可选 `disabled_tools: list[str]`，默认空。未知名拒绝加载。默认 **不**改 `Settings.agent` 现有字段默认值。

`tool_protocol` 本切片不读（那是 V3-B）。

---

## 7. 与现网的关系

| 组件 | V3-A 允许的接触 | 禁止 |
|------|-----------------|------|
| `ReviewStrategy.execute` | 不改签名 | 不在 SinglePass/MultiRole 里调工具 |
| `ReviewService` | `strategy=agentic` → 配置错误退出 | 不装配 AgenticReviewer |
| `FindingPipeline` | 不调用 | stub 不得 enqueue Candidate |
| `ContextAssembler` / L3 | 不改装配；工具自带独立 blob cap | 不把工具结果写进 L3 默认块 |
| `Publisher` / watermark / 反馈 | 不改 | — |
| `LLMProvider` | 不改 `tool_loop` | 不假装 native tools 已通 |
| Storage | 只新增原子 `record_tool_invocation(call, result)` | 不拆成两个公开写入口；不改 findings 生命周期 |

V3-B 将把信封放进 loop：validate → sandbox → execute → observe。V3-A 的公共入口建议：

```text
async def invoke(registry, workspace, name, args, *, tool_call_id=None) -> tuple[ToolCall, ToolResult]
```

---

## 8. 存储与可观测

### 8.1 落库

表已存在。Storage 协议新增（async）：

- `record_tool_invocation(call: ToolCall, result: ToolResult) -> None`

要求 `call.task_id` 非空且 `tasks` 行存在（测试先 `record_run` + task，或允许测试用显式 FK 种子）。无 `task_id` 时信封仍返回，**不写库**。

`record_tool_invocation` 必须在一个 SQLite 事务内校验 task FK 并同时写入 call/result；任一步失败整体回滚，不允许孤立 call。相同 `tool_call_id` + 相同规范化内容视为幂等成功；同 ID 但内容冲突必须拒绝，禁止静默覆盖。

`user_version` 保持 **5**。`tool_results.source` 不单开列。

### 8.2 日志

复用 `StructuredLogger`：`tool_call_id` / `name` / `status` / `duration_ms` / `truncated` / `repeat_of`。日志禁止记录 data 正文；error 最多 256 chars，并先走统一 secret redaction。

`observability.save_tool_trace` 已存在：True 时才允许写库，False 时只内存（默认 True）。即使开启，`args_json/data/error` 仍必须先经统一 secret redaction，data 遵守最终字符硬上限。

`source` 不新增数据库列，写入 data 时固定使用结构化信封 `{"payload": ..., "source": {"kind": "tool", "ref": ..., "sha": ...}}`；内存 `ToolResult.source` 与信封 source 必须一致，避免 V3-B 二次解析产生两套格式。

---

## 9. 安全、超时、降级

- 只读；无命令拼接。
- 路径沙箱见 §5。
- `search_code` 仅字面量搜索；glob 有长度与路径沙箱门槛；V3-A 不执行用户正则。
- 超时不重试（重试是 loop 策略）。
- 取消：尽力记录 cancelled 审计后重新抛出 `CancelledError`；未完成调用不返回常规 ToolResult，迟到结果不得变为可见结果。
- 工具失败 **不是** ReviewRun 失败：本切片无 Run 接线。未来 V3-B 按 `08` 回喂。
- 评测默认 `retain_source_in_evals=false`：compare 报告不贴源码全文。

---

## 10. 评测与测试

### 10.1 必须单测

- 绝对路径 / `..` / 盘符 / UNC / 空 path → `invalid_args`
- resolve 逃逸（含可创建时的 symlink）
- schema 缺字段、类型错、extra forbid
- 未知工具
- `read_file` 行裁剪与 `truncated`
- `find_files` 只返回 snapshot 内路径
- `search_code` 空/过长 query；字面量 metachar 不按正则解释；文件数、单文件字符、总扫描字符和命中数截断
- `find_references` 复用抽取；未知 symbol → 空列表 ok
- `read_diff` 无该文件 → `not_found`
- stub 两件 → `not_bound`
- 超时 Fake handler → `timeout`
- 取消慢 handler → 父 task cancelled；不记录 ok/timeout；审计失败也继续传播
- 落库往返（有 task_id）、事务回滚、同值幂等、同 ID 冲突拒绝、trace off 不落库、secret 脱敏
- `strategy=agentic` Service 构造或 review 入口拒绝

### 10.2 对照 `v3a_compare`

主表不是 P/R/F1：

| 表 | 内容 |
|----|------|
| 越界 | 对抗 path 全集拒绝 |
| 合法读 | Fake snapshot 指定文件可读 |
| 截断 | 超限 `truncated=true` 且长度遵守 |
| stub | 控制工具不进 Pipeline |
| 隔离 | 默认 review 路径不产生 tool_calls |

数据集：`reposage/evals/datasets/v3a_sandbox.yaml`。披露：无真实模型。

V2-A/B/C/D/E compare 钉死 `agent.enabled=false`。

---

## 11. 分步实施任务卡（审查通过后才编码）

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查与 Round 1 修订 | **完成** | — |
| T1 | sandbox 纯函数 + `ToolSnapshot`/adapter + `ContextSourceKind.TOOL` + `ToolCall.task_id` | 路径对抗与统一快照协议单测 | T0 |
| T2 | Registry + Pydantic schema + `invoke` 信封（超时/截断/未知） | 信封单测 | T1 |
| T3 | `read_file` / `find_files` / 字面量 `search_code`（ToolSnapshot；扫描硬上限） | 工具单测 | T2 |
| T4 | `find_references`（复用 V2-B）+ `read_diff`（已解析 diff） | 工具单测 | T2 |
| T5 | stub `submit_finding` / `finish_review`；Service 拒绝 `agentic` | 负向测 | T2 |
| T6 | Storage 原子 `record_tool_invocation`；幂等/冲突/脱敏/trace 开关；不升 `user_version` | 存储与日志测试 | T2 |
| T7 | `v3a_sandbox` + compare；钉死 A–E 的 agent off | `docs/evidence/v3-a-compare.md` | T3 T4 T5 |
| T8 | 全量回归、Ruff、mypy、`git diff --check` | CI | T7 |

T0 已完成；可按 T1 → T8 实施。除 DP-13 的 agentic 失败快外，仍禁止改 Pipeline / Provider.tool_loop / Publisher，Service 不得接入工具执行。

目录（按 `03`）：

```text
reposage/tools/registry.py
reposage/tools/sandbox.py
reposage/tools/invoke.py          # 信封
reposage/tools/read_file.py
reposage/tools/find_files.py
reposage/tools/search_code.py
reposage/tools/find_references.py
reposage/tools/read_diff.py
reposage/tools/submit_finding.py  # stub
reposage/tools/finish_review.py   # stub
reposage/evals/datasets/v3a_sandbox.yaml
reposage/evals/v3a_compare.py
```

不新建 `reposage/review/agent/`。

---

## 12. DoD 与审查清单

### 12.1 交接 DoD（V3-A 子集）

| # | 条目 | 落点 |
|---|------|------|
| 1 | 只读工具：读文件、搜索、引用、读 diff | T3 T4 |
| 2 | ToolDefinition = name/description/schema/permission/limit/timeout | T2 |
| 3 | 路径 resolve 后限制在 workspace；`..` / 绝对 / symlink 逃逸全拒 | T1 T7 |
| 4 | 执行前 schema 校验 | T2 |
| 5 | 控制工具不执行、不进 Pipeline | T5 |
| 6 | 不实现 loop / tool_loop / AgenticReviewer | 全程 |
| 7 | V1/V2 回归 | T8 |

### 12.2 架构自查

- [ ] 未改 `execute` 主签名
- [ ] 未实现 `tool_loop` / Agent 状态机
- [ ] 未默认打开 `agent.enabled`
- [ ] `strategy=agentic` 被拒绝而不是静默 SinglePass
- [ ] 未把 ruff/Judge/反馈做成工具
- [ ] 未读脏工作区；未 `os.walk` 仓库根
- [ ] 未升 `user_version`
- [ ] 未引入 MCP / jsonschema 新依赖
- [ ] 默认 dry-run；不提交、不 push

### 12.3 实现期测试清单

- 路径对抗全集
- schema forbid extra
- 截断与超时
- stub not_bound
- snapshot 与 live 文件内容不一致时，工具看到的是 SHA 内容
- agentic 配置拒绝
- A–E compare 仍绿

---

## 13. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | V3-A = 工具层 only；无 loop、无 AgenticReviewer、无 `tool_loop` 实现 | `12` 把 registry/sandbox 与 loop 分成 a/b |
| **DP-2** | OQ-11 / ADR-005 /「V3 启动前 tool calling 实测」登记为 **V3-B 门禁**，不阻塞本切片 | V3-A DoD 是越界全拒；A/B 都要调用同一信封 |
| **DP-3** | 不做「运行静态检查」工具；ruff 留在 V2-C 管道 | 程序事实不能让模型重跑改写 |
| **DP-4** | 五件只读工具可执行；`submit_finding` / `finish_review` 只注册，执行 `not_bound` | 无 Pipeline 绑定则禁止提交 |
| **DP-5** | Schema 用 Pydantic，不新增 jsonschema 依赖 | 已在栈内；`model_json_schema()` 可喂未来 tool_loop |
| **DP-6** | 未知工具 status=`invalid_args`，error=`unknown_tool` | 不扩枚举；与非法参数同一回收路径 |
| **DP-7** | `read_diff` 无该文件 → `error=not_found`（不是 ok 空串） | 避免与「文件无变更」混淆；ok 空串留给「有文件但 hunk 被裁」若需要再分 |
| **DP-8** | 新增 `ContextSourceKind.TOOL` | 给 ToolResult.source 与 V3-B 证据追溯 |
| **DP-9** | 工具 args `extra="forbid"` | 与 Settings 一致，防夹带路径 |
| **DP-10** | `user_version` 保持 5；source 不落新列 | 表已够 V3-A 审计 |
| **DP-11** | 文件工具只读锁定 `head_sha` 快照，不读 working tree | SHA 锁定（`10` §7）；防未提交机密/漂移 |
| **DP-12** | 工具 blob cap 独立于 L3 `MAX_L3_FILES_READ` | 避免一次 `search_code` 污染 L3 统计 |
| **DP-13** | `review.strategy=agentic` 失败快；默认仍 `single_pass` | 消除静默降级 |
| **DP-14** | 不改 `execute`；Service/Pipeline/Publisher 除 DP-13 外不接线 | 固定管道继续是默认产品 |
| **DP-15** | MCP / 向量 / FTS / Agent prompt 本切片不做 | 范围控制 |
| **DP-16** | V2-D P2 日志 nit、GitHub 评论指令仍不做 | 非本里程碑 |
| **DP-17** | 五个读取工具只依赖统一异步 `ToolSnapshot`；provider/cache 由 adapter 封装 | 消除 `HeadSnapshot | GitSnapshotProvider` 不兼容 union |
| **DP-18** | V3-A `search_code` 只做字面量搜索，并限制文件数/单文件字符/总扫描字符 | 防正则回溯堵塞事件循环，保证工作量有界 |
| **DP-19** | 取消先尽力审计 cancelled，再重新抛出 `CancelledError` | 不吞父级取消，不伪装普通工具结果 |
| **DP-20** | handler 语义裁剪 + 信封最终字符硬上限；结构化输出不做字符串中截断 | 统一安全边界且保持可解析 |
| **DP-21** | tool call/result 同事务、同值幂等、冲突拒绝；trace 全字段脱敏 | 防孤儿记录、覆盖与敏感信息泄漏 |

若需推翻某条，只改本节与对应章节。

---

## 14. 冻结声明

**设计已审查并冻结，可进入实现。** 仍禁止：

1. 不实现 Agent loop、`tool_loop`、会话压缩、MCP。
2. 不改 `execute`，不改 V2-C/D/E 语义，不改 `cross_run_match_key`。
3. 不把 Agent 或增量或评测对照默认打开。
4. 不让 stub 工具写入 Finding。
5. 不提交、不 push、不打 tag（除非用户明确要求）。

V3-B 开始前必须单独对齐，并完成 OQ-11（或书面接受方案 B / 换模型 / 暂缓 Agent）。
