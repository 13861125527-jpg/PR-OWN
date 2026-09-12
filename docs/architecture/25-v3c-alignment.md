# V3-C 设计对齐（会话压缩 + 证据索引）

> 状态：**ACCEPTED**（实现验收：`docs/evidence/v3-c-implementation-review-round2.md`）
> 前置：V3-B **ACCEPTED**（`docs/evidence/v3-b-implementation-review-round2.md`）
> 用户授权：V3-B 验收后进入 V3-C（本对齐稿按 **V3-C** 执行；V1-C 早已完成）
> 契约来源：`01` FR-24/FR-25、`04` `maybe_compact`、`05` `AgentMessage.is_compressed` / `evidence_index`、`06` §1 最小 L3 / §4 会话压缩、`08` §6、`10` §3 不可信边界、`11` §8 V3 压缩子集、`12` V3-c、交接 `docs/evidence/cursor-handoff-v1-complete-v2-v3-next.md` §8 V3-C、V3-B `24` DP-4/DP-15/DP-22
> DoD 摘要：**程序化 `maybe_compact`；`evidence_index` 完整保留且 Finding 可追溯到原始 tool_call；确定性最小 L3 不被模型摘要替代；超限仍可 PARTIAL `context_overflow`；默认 `agent.enabled=false`；不升 `user_version`；不调用真实模型；V1–V3-B 回归通过**

---

## 0. 一句话与边界

V3-C 把 V3-B 的硬停机 `PARTIAL / context_overflow` 换成 **有界、可追溯的会话压缩**：当轨迹估算超过阈值时，程序把已消费的工具观察聚合成带 `is_compressed` 的摘要消息，并在会话内维护完整 `evidence_index`。压缩 **不调用模型**，不占用 provider round，不把摘要当成新的 L3 证据。

**本里程碑禁止**：用 LLM 写压缩摘要、用压缩文本替换初始 L1/L2/最小 L3、跨文件 V2 vs V3 质量收益报告（V3-D）、MCP、写工具、默认打开 `agent.enabled`、升 SQLite `user_version`、复制审查主流程、在 loop 内推进 Finding 正式状态、恢复 OQ-11 live（仍 `DEFERRED_BY_USER`）。

---

## 1. 现有可复用组件盘点

| 层 | 组件 | V3-C 用法 | 缺口 |
|---|---|---|---|
| 会话 | `AgentSession.messages` / `evidence_refs: dict[str, str]` | 压缩读写 messages；`evidence_refs` 升级为索引 | 无 `maybe_compact`；无 `checked/excluded/pending`；无完整 `Evidence` 索引 |
| 消息 | `AgentMessage.is_compressed` / `summary_ref` | 压缩消息置位 | loop 未使用 |
| 证据 | `Evidence`（`kind/location/content/tool_call_id/verified`） | `evidence_index` 值类型；`submit_finding` 继续引用 `tool_call_id` | 目前只存 `source.ref` 字符串 |
| Loop | 发请求前 `message_chars() > 200_000` → `context_overflow` | 先 compact，仍超硬顶才 overflow | 无阈值、无尾窗口、无 native 配对策略 |
| 协议 | `messages_to_chat` native 要求 assistant `tool_calls` 与 tool 观察 id 配对 | 压缩必须保持或整段丢弃配对 | 压缩前缀若只删 tool 消息会 400 |
| 工具 | `invoke` 信封 + `save_tool_trace` | 全文仍可在 `tool_results`；索引不复制大 payload | 索引内容策略未写 |
| 上下文 | `unit_to_messages` 初始 user（L1/L2，及开启时的最小 L3） | **永不压缩替换** | 压缩器需识别“初始任务消息” |
| 配置 | `Settings.agent` | 增加 compact 阈值与尾窗口 | 现无 compact 项 |
| Prompt | `prompts/tasks/agent.md` | 补一句：压缩摘要不是新证据 | 未提及 compact |
| 存储 | `user_version=5`；Session 不落库 | 保持 | `06`「压缩摘要落库」需本切片裁决 |
| 评测 | `v3b_compare` 14/14 | 保留；新增 `v3c_compare` | 无 compact 反例 |
| 安全 | `wrap_untrusted` / 脱敏 | 压缩事实块仍包 UNTRUSTED | 指令句与事实块必须分开 |

**明确不复用**：LangChain 记忆压缩、把 Judge/模型当成 summarizer、把 compact 文本写进 system。

---

## 2. 产品目标与非目标

### 2.1 目标（FR-24 / FR-25 的本切片；`12` V3-c）

1. **触发压缩**：估算 token（沿用 `chars/4`）超过会话阈值，或单条观察超长时，调用 `maybe_compact`，而不是立刻 overflow。
2. **程序摘要**：已消费 tool 观察聚合成稳定格式（工具名、`tool_call_id`、路径/范围、ok/error、truncated、source.ref）。**禁止**再发一次 provider 请求让模型“总结刚才读到了什么”。
3. **证据索引**：`evidence_index[tool_call_id] = Evidence` 在压缩后完整保留；`submit_finding` 只认索引里已成功的 id；压缩不得使已提交候选的证据 id 失效。
4. **最小 L3 不被替代**：初始任务 user 消息（程序装配的 L1/L2/最小 L3）原样保留。工具读到的代码可以从 transcript 撤下，但不得用模型散文冒充那份初始上下文。
5. **可追溯**：压缩后提交的 Finding 仍能通过 `evidence.tool_call_id` 回到原始 ToolCall/ToolResult（内存索引 + 可选 DB 行）。
6. **硬顶仍在**：压缩后仍超过硬字符上限 → `PARTIAL / context_overflow`；已提交候选保留（V3-B 语义）。
7. **可测**：Fake 轨迹把会话做大再 compact；不依赖真实 LLM。
8. **不破坏默认路径**：`agent.enabled=false`；V3-B 状态机/预算/grace/repair/取消契约不变。

### 2.2 非目标（本里程碑硬裁）

| 不做 | 归属 | 理由 |
|------|------|------|
| LLM 摘要 / CoT 压缩 | — | 会把幻觉写进“已验证事实” |
| 跨文件 V2 vs V3 P/R/F1 对照 | V3-D | `11` §8 全量 V3；无稳定压缩前不宣称收益 |
| MCP、写工具、ruff-as-tool | — | 与 V3-A/B 相同 |
| 崩溃后从压缩摘要恢复半截 AgentSession | — | 延续 V3-B DP-22 |
| 升 `user_version` / 新表存 compact blob | 除非审查推翻 DP-3 | 范围控制 |
| 默认打开 Agent / live OQ-11 | — | Round 2 明确后置 |
| 改 V2-C/D/E、fingerprint、`execute` 签名 | — | 回归铁律 |

### 2.3 与架构原文的已知差异（必须显式）

| 架构原文 | V3-C 裁决 | 处理 |
|----------|-----------|------|
| `08` 压缩示例「`a.py L20-40 → 定义 X`」 | 「定义 X」若由模型生成即违规 | **DP-1**：结论字段只允许程序从信封取出的 path/range/status，不写语义定义 |
| `06` 会话记忆「内存 + 压缩摘要落库」 | Session 仍不持久化 | **DP-3**：只记 `agent.compact` 结构化日志（计数/阈值），不落全文 |
| `04` 仅在 `tool_call` 后 `maybe_compact` | 发请求前也必须压 | **DP-6**：每轮 reserve 前 + 观察回灌后各检查一次 |
| `05` `evidence_index: dict[str, Evidence]` | 落地；`evidence_refs` 改为索引的派生视图或删除内部用法 | **DP-2** |
| `08` §7 终局重跑 Pipeline | 仍只在 Service `execute` 之后 | 沿用 V3-B DP-3 |
| V3-B DP-4 超限直接 overflow | 改为 compact 失败才 overflow | 本切片关闭 DP-4 |

---

## 3. 领域模型

### 3.1 `evidence_index`

```text
AgentSession.evidence_index: dict[str, Evidence]
  key   = tool_call_id
  value = Evidence(
      kind=TOOL_RESULT,
      location=source.ref,          # 如 tool:read_file:<id>
      content="",                   # 不复制大 payload；见 DP-2
      tool_call_id=<id>,
      verified=True,                # 仅 ToolCallStatus.OK
    )
```

- 成功工具写入/更新索引；unknown / invalid_args / repeat / timeout / error / grace 拒绝 **不** 进索引。
- `submit_finding`：`evidence_tool_call_ids` 必须全部在索引中；用 `Evidence` 对象填候选，不再只塞 location 字符串。
- 压缩过程 **不得删除** 索引条目。
- 现有 `evidence_refs` 若保留，必须与索引 `location` 一致，避免两套真相；实现期优先只保留 `evidence_index`，测试同步改引用。

### 3.2 会话集合（`06` §4）

从已执行工具 **程序推导**，不让模型维护：

| 集合 | 含义 | 来源 |
|------|------|------|
| `checked` | 已成功观察的定位键 | 如 `read_file:src/a.py:L1-20`、`search_code:needle` |
| `excluded` | 明确失败/越界/not_found | 信封 error code + path |
| `pending` | 本切片恒为空或仅含「未执行的 extra native calls」 | 轨迹内顺序执行，不发明第二执行队列 |

压缩摘要里序列化这三个集合（有界、排序、截断条数上限），便于模型避免重复探索。

### 3.3 压缩消息

一条（或替换先前那一条）user 消息：

- `role=user`
- `is_compressed=true`
- `summary_ref=compact-<n>`（会话内单调）
- `content` = 短指令（程序）+ `wrap_untrusted(JSON 事实块)`

事实块字段（稳定 sort_keys）：`tool_call_id`、`name`、`status`、`ref`、`path`（若有）、`start_line`/`max_lines`（若有）、`truncated`、`error`（脱敏截断）。

**不包含**：源码正文、完整 search hits、模型散文、secret。

### 3.4 配置

`Settings.agent` 新增（默认关闭 Agent 行为不变）：

| 字段 | 默认 | 约束 |
|------|------|------|
| `compact_threshold_ratio` | `0.60` | `(0, 1]`；相对 `max_session_chars` |
| `compact_keep_rounds` | `2` | `>= 0`；未压缩的尾部完整轮数（assistant+观察） |
| `max_session_chars` | `200000` | `>= 4096`；硬顶，超过且 compact 无效 → overflow |

阈值 token ≈ `message_chars() / 4`，与 loop 现估算一致。单测可把 `max_session_chars` 降到很小以强制触发。

---

## 4. `maybe_compact` 算法

纯函数优先：`(session) -> CompactResult`，loop 只负责调用时机与打日志。

### 4.1 何时运行

1. **发 provider 请求之前**（reserve 前）：保证送进模型的 transcript 已低于阈值。
2. **工具观察回灌之后**（回到 RUNNING 后）：避免下一轮才发现爆炸。
3. **不在 WAITING_TOOL 中**压缩：invoke 尚未回灌，配对未完成。
4. JSON 修复 pending 时仍可压缩（修复也是一次逻辑请求，应看到较短上下文）。
5. grace 模式允许压缩（只缩小上下文，不因此重新开放只读工具）。

### 4.2 保留 / 折叠

**永远保留（相对顺序不变）**

1. 全部 `system` 消息。
2. **第一条 user 任务消息**（初始 L1/L2/最小 L3）。若其后已有 `is_compressed` 摘要，保留最新一条压缩消息，丢掉更旧的压缩消息（避免摘要叠摘要）。
3. 尾部 `compact_keep_rounds` 个 **完整轮**：从末尾向前数，每轮 = 一条带 `tool_calls` 的 assistant（或控制动作 assistant）+ 其配对 tool/user 观察。不完整轮不拆开。

**折叠进摘要**

- 上述窗口之外的 assistant / tool / 普通 user 观察（含 idle/invalid_action 回灌）。
- 被折叠的 native 轮必须 **整轮删除**（assistant `tool_calls` 与对应 tool 消息一起），不得留下悬空 `tool_call_id`（**DP-4**）。

**不折叠**

- `session.calls`、`candidates`、`evidence_index`、集合、预算计数。

### 4.3 单条超长

若某一条仍留在尾窗口的 tool 观察超过硬顶的一小段（建议 `max_session_chars / 8`）：

- 保留 native 配对结构；
- 将该条 `content` 换成短 stub（id + ref + `compacted_payload=true`）；
- 完整 payload 仍以 DB/`evidence_index` 为准。

### 4.4 停止条件

| 结果 | 行为 |
|------|------|
| 未超阈值 | no-op |
| 压缩后低于阈值 | 打日志 `agent.compact`，继续 loop |
| 已无可折叠轮且仍超硬顶 | `PARTIAL / context_overflow` |
| 压缩本身异常 | 不吞取消；其它错误 → 当作无法压缩，走 overflow，不崩溃成 FAILED（避免丢已有候选） |

压缩 **不计** `rounds_used` / `grace_rounds_used` / GlobalBudget reserve。

### 4.5 与 Action JSON / native

同一套 session 消息；`messages_to_chat` 不改协议分支语义。压缩后的前缀变成 user 摘要，尾窗口仍按当前 `tool_protocol` 配对。Fake 不解析摘要，只继续剧本。

---

## 5. 最小 L3 与“不能被模型摘要替代”

`06` §1：V3 保留 V2 的确定性最小 L3 作为 **初始上下文**，再用工具增量探索。

本切片铁律：

1. 初始 user 消息字节级保留（可继续带原有 UNTRUSTED 包装）。
2. 压缩摘要 **显式声明**「不是新证据、不能替代初始上下文或 tool_results」。
3. `submit_finding` 不得把 `summary_ref` 当成 `evidence_tool_call_id`。
4. Pipeline / location 校验仍走 snapshot + 候选字段，不读压缩散文。

工具读到的文件可以从 transcript 撤下；这不是丢 L3，而是把增量 L3 从 prompt 挪到索引。初始最小 L3 仍在第一条 user 里。

---

## 6. Loop 接入（相对 V3-B 的最小改动）

伪代码（只展示增量）：

```text
while RUNNING:
    if wallclock / chars 硬顶:
        maybe_compact()
        if 仍超硬顶: PARTIAL context_overflow
    maybe_compact()                    # 阈值
    # ... grace / round / reserve / tool_loop （V3-B 不变）
    handle_action / 回灌观察
    maybe_compact()
```

V3-B 的 grace、repair 不借 finalize、settle 幂等、取消重抛、多 tool_call 配对全部保持。

`submit_finding` 查 `evidence_index` 而非 `evidence_refs`。

---

## 7. Prompt / 安全 / 可观测性

- `prompts/tasks/agent.md` 增加：若出现「已压缩历史摘要」，只可引用其中的 `tool_call_id`；禁止把摘要当作读过的源码。
- 压缩指令句放在 UNTRUSTED 外；事实 JSON 在内（`10` §3）。
- 日志 `agent.compact`：`before_chars`、`after_chars`、`folded_rounds`、`index_size`、`summary_ref`。禁止写源码/prompt/Key。
- 不保存 raw CoT；压缩不是 CoT 转储。

---

## 8. 存储与兼容

- `user_version` 保持 **5**。
- Session / compact blob / 集合不落库。
- `tool_calls` / `tool_results` 仍按 V3-A/B 审计；这是压缩后全文追溯的磁盘来源（`save_tool_trace=true` 时）。
- `save_tool_trace=false` 时：内存 `evidence_index` 仍可提交候选；评测/对照默认开 trace。

---

## 9. 评测与测试

### 9.1 Fake 必须覆盖

- 超阈值 → 出现恰好一条最新 `is_compressed` user 消息；初始 user 仍在且内容不变。
- 压缩后 `evidence_index` 条目数 = 成功工具数；随后 `submit_finding` 引用压缩前的 id 仍成功。
- 压缩不得调用 `llm.tool_loop`（call count 不因 compact 增加）。
- native：压缩后下一轮 `messages_to_chat` 无悬空 `tool_call_id`。
- 尾窗口 `compact_keep_rounds=1` 时，最后一轮 assistant+tool 仍未折叠。
- 已无可折仍超硬顶 → `PARTIAL / context_overflow`。
- 单条超长观察 → stub，配对仍在。
- 集合 `checked` 含成功 read 的定位键；`not_found` 进 `excluded`。
- 默认 `single_pass` 仍零 tool_calls；V3-B 轨迹 compare 仍 14/14。
- 直接 `invoke(submit_finding)` 仍 `not_bound`。

### 9.2 对照 `v3c_compare`

数据集 `reposage/evals/datasets/v3c_compact.yaml`，脚本化 Fake，无真实 API。

| id | 断言 |
|----|------|
| compact-keeps-min-l3 | 初始 user 未变 |
| compact-keeps-evidence-ids | 压缩后 submit 用旧 id |
| compact-not-a-round | provider 调用次数不因 compact +1 |
| compact-native-pairing | chat 转换无悬空 id |
| compact-recompact-keeps-old-facts | 第二次压缩仍含第一次摘要中的 tool_call_id |
| compact-json-repair-keeps-repair-instruction | keep_rounds=0 时仍保留 JSON repair 指令 |
| overflow-after-compact | 硬顶仍 overflow |
| default-agent-off | 默认路径无 compact/tool |

报告：`docs/evidence/v3-c-compare.md` / `.json`。主表不是 P/R/F1。

---

## 10. 分步实施任务卡

| 卡 | 内容 | 验收 | 依赖 |
|----|------|------|------|
| T0 | 本对齐稿审查与修订 | 本文冻结 | — |
| T1 | `evidence_index` + 集合；`submit_finding` 改走索引 | 单测：压缩前路径与 V3-B 行为兼容 | T0 |
| T2 | `maybe_compact` 纯函数（保留/折叠/stub/native 配对） | 无 LLM 的单元测试 | T1 |
| T3 | loop 接入时机；`context_overflow` 仅在 compact 无效后 | loop 单测 | T2 |
| T4 | `AgentConfig` 三字段；prompt 一句；日志 `agent.compact` | 配置校验 + 日志无源码 | T3 |
| T5 | `v3c_compare` + 钉死 v3b 14/14 与 A–E / V3-A | `docs/evidence/v3-c-compare.md` | T4 |
| T6 | 全量 pytest / Ruff / mypy | CI | T5 |

目录（预计）：

```text
reposage/review/agent/compact.py
reposage/evals/v3c_compare.py
reposage/evals/datasets/v3c_compact.yaml
docs/evidence/v3-c-compare.md
docs/evidence/v3-c-status.md
```

改动点：`session.py`、`loop.py`、`settings.py`、`prompts/tasks/agent.md`、`tests/test_agent.py`（或新 `tests/test_agent_compact.py`）。

---

## 11. DoD 与审查清单

### 11.1 里程碑 DoD

- [ ] `maybe_compact` 程序化，零额外 provider 请求
- [ ] `evidence_index` 压缩后仍完整；Finding 能回溯 `tool_call_id`
- [ ] 初始最小 L3/L1/L2 user 不被替换
- [ ] native 压缩后 transcript 可发给 chat.completions
- [ ] 硬顶 overflow 仍存在
- [ ] 默认 Agent 关闭；无 live API
- [ ] `user_version == 5`
- [ ] V3-B compare 14/14 与全量回归绿

### 11.2 实现期禁止项

- [ ] 不调用模型做摘要
- [ ] 不把 compact 文本写入 system
- [ ] 不升 schema
- [ ] 不开始 V3-D 对照实验
- [ ] 不默认 `agent.enabled=true`

---

## 12. 决策点（未遭反对则按此实现）

| ID | 结论 | 理由 |
|----|------|------|
| **DP-1** | 压缩 100% 程序生成；禁止 LLM summarizer | 交接：最小 L3 不能被模型摘要替代 |
| **DP-2** | `evidence_index` 存 `Evidence`；`content` 不复制大 payload；全文以 tool 信封/DB 为准 | 可追溯不等于把文件再塞进索引 |
| **DP-3** | 压缩摘要不落库；只打计数日志 | 与 V3-B DP-15/22 一致，不升版本 |
| **DP-4** | 折叠 native 轮必须整轮删除，禁止悬空 tool id | 否则下一轮 provider 400 |
| **DP-5** | 初始第一条 user 永不折叠 | 保护确定性最小 L3 |
| **DP-6** | compact 在 reserve 前与观察后执行；WAITING_TOOL 中不压 | 配对完整前不能改 transcript |
| **DP-7** | compact 不计 round、不 reserve GlobalBudget | 不是模型请求 |
| **DP-8** | 仍超硬顶 → `PARTIAL / context_overflow` | 关闭 V3-B「未压缩就 overflow」但不取消硬顶 |
| **DP-9** | `checked/excluded/pending` 由程序从工具结果推导 | 模型不可改集合 |
| **DP-10** | `agent.enabled` 默认 false；OQ-11 live 仍后置 | Round 2 约束 |
| **DP-11** | 不改 V2 语义、不改 `execute` 签名、不改 fingerprint | 回归铁律 |
| **DP-12** | 重复压缩替换同一摘要槽，不无限追加压缩消息 | 防止摘要自己把上下文撑爆 |
| **DP-13** | `compact_keep_rounds` 默认 2 | 给模型留最近观察；可配 |

若需推翻某条，只改本节与对应章节。

---

## 13. 审查时请确认

1. DP-1（禁止 LLM 摘要）是否接受；若希望「可选的一次便宜总结请求」，那是范围扩大，需另开决策。
2. DP-3（摘要不落库）是否接受；若必须落库，将被迫升 `user_version`。
3. 默认 `compact_keep_rounds=2` 与阈值 0.60 是否可直接实现。
4. 本切片是否继续 **不** 跑真实 API。

实现按 §12 DP；验收审查见 `docs/evidence/v3-c-status.md`。
