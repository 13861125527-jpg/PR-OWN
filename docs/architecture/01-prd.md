# 01 — 产品需求文档（PRD）

> 用户 / 场景 / 功能与非功能需求 / 非目标 / 成功指标。
> 标注：`V1 now` / `V2 later` / `V3 later` / `optional`。

---

## 1. 用户画像

| 用户 | 诉求 | 使用方式 |
|------|------|----------|
| 个人开发者（主用户） | 不想在低质量 PR 上耗时间；担心自己漏掉安全/边界问题 | 本地 CLI 跑 dry-run；确认后发布评论 |
| 小型团队 | 统一的审查基线，减少 reviewer 的主观差异 | GitHub Action 自动触发（v1.0.0） |
| 面试展示（本项目自身） | 展示真实工程能力：Git 工作流、Agent、评测 | 演示仓库 + 线上冒烟（v1.0.0） |

## 2. 使用场景

### 场景 1：本地 dry-run（V1 now）
开发者在本地仓库执行 `reposage review --pr 42` 或 `reposage review --base main --head feat/x`，工具完成全部分析，输出 PR 摘要、风险等级、行内评论计划到终端与 SQLite。

**dry-run 的准确语义（P1-1）**：不执行任何 GitHub **写操作**（不发布评论、不推进任何远程状态），但仍可能**读**取 GitHub（`--pr` 模式需拉取 PR 元数据与 diff）；`--base/--head` 本地模式可完全离线（不访问 GitHub）。

### 场景 2：正式发布（V1 now）
`--publish` 显式开启后，按 Saga/Outbox 状态机幂等发布（见 `07` §7）：已发布过的稳定指纹不会重复评论（重复运行同 PR 结果为空增量）；部分失败可恢复。

### 场景 3：多视角审查（V2 later）
PR 变更特征（语言、文件类型、改动模式）触发相应角色（security、silent-failure 等），多个角色异步并发后汇聚、去重、排序。

### 场景 4：主动取证（V3 later）
模型在审查中不确定某符号含义或跨文件影响时，调用只读工具（read_file/find_references/search_code 等）取回证据后再提交 Finding；找不到证据则降低置信度或放弃。

### 场景 5：误报反馈（V2 later）
用户标记某评论为误报/wont-fix，仓库后续审查参考该记忆并可撤销（CLI 命令与 GitHub Comment 指令，入口取舍见 `14` OQ-4）。

### 场景 6：后台自动运行（v1.0.0）
GitHub Action 在 push/open 事件上自动运行，结果以 review 评论呈现；默认仍保持 dry-run 语义（只输出 summary comment，除非显式开启 line comments）。

## 3. 功能需求（FR）

| 编号 | 需求 | 版本 | 验收要点 |
|------|------|------|----------|
| FR-1 | CLI 接收本地 base/head ref 或 GitHub PR URL | V1 | 两种输入等价产出 |
| FR-2 | GitHub Token 从环境变量读取，不进入日志/Prompt | V1 | 日志审计无 token |
| FR-3 | 获取 PR、锁定目标 head SHA、获取 changed files 与 unified diff | V1 | head SHA 全程锁定 |
| FR-4 | diff parser：文件、hunk、新旧行号、增删改、重命名 | V1 | 单元测试覆盖 |
| FR-5 | 文件过滤：生成文件、lock、二进制、超大、不支持语言 | V1 | 过滤规则可配置 |
| FR-6 | 按 hunk/符号分块，禁止固定字符截断 | V1 | 分块是 token-aware 的 |
| FR-7 | 单通用 reviewer 产出严格 Finding Schema | V1 | schema 校验 |
| FR-8 | 程序验证路径、目标 SHA、diff 新增行、严重度/置信度 | V1 | 非法 Finding 被拒或降级 |
| FR-9 | PR 摘要与行内评论计划 | V1 | 摘要 + 行内计划分离 |
| FR-10 | dry-run 与显式正式发布；幂等 | V1 | 重复运行零增量 |
| FR-11 | 异步 I/O、有限文件并发、semaphore | V1 | 并发上限可配置 |
| FR-12 | ReviewRun 记录、结构化日志、Token/费用/耗时、覆盖清单 | V1 | SQLite 落库 |
| FR-13 | Fake Git/LLM Provider、单元/端到端测试、基础评测集 | V1 | CI 可跑 |
| FR-14 | 角色注册表与确定性条件门控 | V2 | 门控可复现 |
| FR-15 | 多角色异步并发、全局/Provider 分级限流 | V2 | semaphore 分层 |
| FR-16 | AST/Tree-sitter 符号级预检索上下文 | V2 | 符号缓存可失效 |
| FR-17 | 1–2 个高价值静态分析器进入统一 Finding | V2 | 结果带来源 |
| FR-18 | Finding 规范化、聚类、去重、来源合并、排序 | V2 | 重复率下降可测 |
| FR-19 | Judge 仅 keep/downrank；needs_evidence 为内部标记 | V2 | 不能伪造事实 |
| FR-20 | 增量 SHA watermark + Saga 发布 | V2 | 失败可恢复 |
| FR-21 | 误报/wont-fix 可撤销仓库反馈记忆 | V2 | 可查看/撤销 |
| FR-22 | 受控只读工具与 Agent event loop | V3 | 工具 schema 校验 |
| FR-23 | 多轮、预算（轮数/Token/费用/工具次数/墙钟）、取消、grace round | V3 | 状态机全覆盖 |
| FR-24 | 会话记忆压缩与证据索引 | V3 | 压缩后仍可追溯 |
| FR-25 | 每个保留 Finding 可追溯到 diff 或工具证据 | V3 | 证据 groundedness 评测 |
| FR-26 | 跨文件评测证明 V3 相对 V2 收益 | V3 | 对照实验报告 |

## 4. 非目标（硬约束）

| 非目标 | 说明 |
|--------|------|
| 自动修复并提交代码 | 首期不做；suggested patch 不自动应用 |
| GitLab/Bitbucket 等多平台 | Git Provider 抽象预留，但不实现 |
| 向量数据库/GraphRAG/代码图平台 | SQLite 结构化索引足够；评测信号出现再评估（见 `06` §6） |
| 多自主 Agent 长对话 | 单 Agent + 任务级并发 |
| 模型微调 | 不纳入 |
| 微服务/Kubernetes | 与个人项目规模不匹配 |
| LangChain/LangGraph 强依赖 | 见 `03` §7 论证 |

## 5. 非功能需求（NFR）

| 类别 | 需求 | 可验证指标（验收门槛见 `11` §8） |
|------|------|------|
| 安全 | 只读、沙箱、无 secret 泄漏、无注入越权 | 安全测试用例全绿 |
| 可靠 | 幂等、部分成功、失败可恢复 | 注入故障后重放通过 |
| 成本 | 预算硬上限 | 超预算即停止（grace round 例外） |
| 性能 | 常规 PR 端到端 < 5 分钟（模型耗时主导） | 记录阶段耗时 |
| 可观测 | 全程可追溯：run/task/tool/finding ID | trace 完整 |
| 可维护 | 分层依赖方向、类型检查、lint | mypy/Ruff 通过 |
| 隐私 | 日志与评测数据去敏策略 | 见 `10` §10 |

## 6. 成功指标（分层）

- **V1 可用**：能在真实小型 PR 上 dry-run 出 ≥1 条有效行内评论；零发布事故（重复评论/越界行号）。
- **V2 质量**：评测集 Precision ≥ 0.7（角色子集定位）；安全/静默失败类命中 Recall 提升可对照；重复率下降。
- **V3 增值**：跨文件样本上，V3 相对 V2 的 Recall/groundedness 提升有评测报告背书；工具有效率 ≥ 0.7、重复调用率 < 0.3。
- **产品形态（v1.0.0）**：Action 接入演示仓库，线上冒烟 10 次无失败发布。

> 具体数值为设计初值，须以真实评测校准（`14` OQ-5）。
