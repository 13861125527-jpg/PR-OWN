# V3-B 对齐稿审查与修订（Round 1）

> 审查对象：`docs/architecture/24-v3b-alignment.md`  
> 结论：**APPROVED DESIGN / 可进入实现**  
> 日期：2026-08-16

## 1. 结论

V3-B 的总体切片正确：本阶段实现受控 Agent Loop、双工具协议、状态机、预算与控制工具绑定，不提前实现 V3-C 压缩或 V3-D 质量对照。审查发现的接口与并发矛盾已直接修改进主稿，无需 Cursor 再写一轮对齐回复。

修订后的主稿可作为 T1 → T8 的实现契约。

## 2. 本轮发现并已修订的问题

### 2.1 Provider 类型依赖方向

原稿要求 domain 的 `LLMProvider.tool_loop` 直接接收 review 层具体 `AgentSession`，会形成 domain→review 反向依赖。

已改为：

- domain 定义只读结构协议 `AgentSessionView` 和 `AgentToolRequest` DTO；
- review 层具体 `AgentSession` 实现该协议；
- Provider 只读取 view，不得修改 candidates/calls。

### 2.2 AgenticReviewer 无法从现有 execute 参数构造 Workspace

`execute(units, run, budget)` 拿不到 Service 的 `file_map`、snapshot provider 与本地 sandbox root，尤其不能可靠构造 `read_diff`。

已新增每次运行的 `AgentWorkspaceFactory`：

- Service 在 FETCH/CONTEXT 后，用锁定 snapshot + 结构化 file_map 构造；
- AgenticReviewer 从 factory 获取 workspace，不解析 prompt/L2 文本；
- 本地模式使用真实 sandbox root；远程/Fake 使用 `sandbox_root=None` + snapshot 路径集合权威；
- 每次 review 使用局部 active strategy，不覆盖共享 `self.strategy`，防并发 run 串 head/diff。

### 2.3 ToolCall FK 与 WAITING_TOOL 审计

原稿说任务最终由 Service 落库，但 V3-A 工具审计要求 `tool_calls.task_id` 已存在，首次 invoke 会违反 FK。

已改为：

- AgentTask 进入 RUNNING 时先落库；
- 每次状态转换按同一 task_id upsert；
- 首次 invoke 前 task FK 必然存在；
- WAITING_TOOL、CANCELLED 与终态具有审计记录；
- Session 消息不持久化，仍不做跨进程恢复。

### 2.4 取消语义不可能同时“抛异常并返回候选”

原稿要求 `CancelledError` 继续传播，同时让候选随 StrategyResult 返回，两者不能同时成立。

已改为：

- 尽力记录 AgentTask=CANCELLED 后重新抛出；
- 取消路径不返回 StrategyResult；
- 会话 Candidate 尚未进入 Pipeline，不承诺保留；
- 父取消必须取消并等待 sibling 文件 Agent，不能被 gather 降级成普通 PARTIAL。

### 2.5 Native 工具调用 transcript 不完整

仅保留并执行 `tool_calls[0]` 会丢失 provider call id，下一轮 native API 可能因 assistant/tool 消息不配对而拒绝请求。

已改为：

- 归一响应保存完整 tool_requests；
- Session 保存完整 assistant native action；
- 执行第一件；
- 其余每个 call id 都追加 `not_executed_multiple_calls` tool observation；
- 每条 tool observation 必须与 assistant call id 配对。

### 2.6 多文件并发会复制 finalize 预留

若每个 Session 各自按剩余全局预算预留 10%，N 个文件会虚构 N 份 finalize 额度。

已新增 run 级共享 `FinalizeBudgetCoordinator`：

- 一个 AgenticReviewer run 只有一份 finalize 池；
- 探索请求必须保护这份共享池；
- grace 请求竞争共享池；
- GlobalBudget 仍是最终原子硬顶。

### 2.7 错误/修复请求可能免费增加模型调用

原稿将 Action JSON 修复视为“同一轮”，可能绕开 max_rounds。

已改为：

- 每次实际 provider 逻辑请求都计 round，包括 JSON 修复与 grace；
- 工具动作另计 `tool_attempt_count`；
- 新增 `max_tool_attempts`，默认 `max_tool_calls * 3`；
- provider 最终超时也占逻辑轮；GlobalBudget/wallclock 继续兜底。

### 2.8 OQ-11 真实模型门禁

用户此前已明确“真实模型先不做”。主稿原先仍将 live 证据设为 V3-B ACCEPTED 强制依赖，与用户决定冲突。

已改为：

- T1 交付可运行探针和 `DEFERRED_BY_USER` 报告；
- 本轮不读取 Key、不调用真实 API；
- V3-B 以 Fake/Provider 单测完成工程验收；
- Agent 默认关闭并标记 experimental；恢复 live 前不得宣称真实模型协议已验证。

## 3. 保留的关键边界

- Strategy 只返回 FindingCandidate，Pipeline 仍是正式 Finding 生命周期唯一所有者。
- loop 不直接调用 Pipeline。
- submit_finding 由 loop 拦截，V3-A stub 继续 not_bound。
- finish_review 只结束会话，不做正式 Finding 校验。
- grace 禁止新只读工具，无成功工具证据不能提交候选。
- 单文件轨迹顺序执行，文件间受 file_tasks/model_requests 限流。
- 不实现会话压缩、MCP、写工具、ruff-as-tool、V3-D 质量门槛。
- 默认 single_pass、agent.enabled=false，不自动升级用户。

## 4. 实现验收重点

实现后除主稿 §10 测试外，重点复验：

1. AgentTask 在首次 tool invoke 前已落库，FK 不失败。
2. 两个并发 review 不串 snapshot SHA 或 diff_files。
3. 远程 workspace 不依赖伪造本地目录。
4. native 多 tool_calls 的 assistant/tool 配对完整。
5. 父取消传播并清理 sibling tasks。
6. 多文件只共享一份 finalize 池，任何路径不突破 GlobalBudget。
7. JSON 修复与错误动作不能绕过 round/tool-attempt 硬顶。
8. 默认 V1/V2/V3-A 路径零 Agent 副作用。

## 5. 最终判定

**V3-B 对齐稿 Round 1：审查通过，已直接修订并冻结。**

可以按 `24-v3b-alignment.md` 的 T1 → T8 开始编码。
