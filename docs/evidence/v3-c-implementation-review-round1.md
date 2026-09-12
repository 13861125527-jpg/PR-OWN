# V3-C 实现审查（Round 1）

> 审查日期：2026-08-16  
> 审查结论：**CHANGES REQUIRED / 暂不验收**  
> 审查基线：`docs/architecture/25-v3c-alignment.md`

## 1. 总结

V3-C 的主体方向是对的：压缩是程序生成的，没有调用模型；`evidence_index` 已接入；loop 在发请求前和工具观察后调用 compact；默认 Agent 仍关闭；`user_version` 没升级。现有门禁也全部通过。

但当前实现仍有一个必须修复的 P1：**重复压缩会丢掉旧压缩摘要里的 facts/tool_call_id**。这会让模型在第二次压缩后看不到早期工具证据 id，破坏 V3-C 的核心目标“压缩后仍可追溯并引用原始 tool_call_id”。因此本轮不能验收。

## 2. 验证结果

| 检查 | 结果 |
|---|---|
| 全量 `pytest -q` | PASS（1 skipped） |
| `python -m reposage.evals.v3c_compare` | PASS（6/6） |
| `python -m reposage.evals.v3b_compare` | PASS（14/14） |
| `ruff check .` | PASS |
| `mypy reposage` | PASS（102 source files） |
| OQ-11 live | DEFERRED_BY_USER |

说明：compare / Ruff / Mypy 首次运行受沙箱写权限影响失败；提权后同一命令通过。失败原因是无法写 `docs/evidence` / `.ruff_cache` / 类型检查缓存，不是代码失败。

## 3. 必须修复（P1）

### P1-1：重复压缩会丢失旧摘要中的 facts/tool_call_id

位置：

- `reposage/review/agent/compact.py:67`
- `reposage/review/agent/compact.py:81`
- `reposage/review/agent/compact.py:83`
- `reposage/review/agent/compact.py:135`
- `reposage/review/agent/compact.py:136`
- `reposage/review/agent/compact.py:148`

当前逻辑：

1. `_partition_rest()` 会把已有压缩消息收集到 `compressed`。
2. `maybe_compact()` 调用时写成 `_, rounds = _partition_rest(rest)`，直接丢弃旧压缩消息。
3. 新摘要的 `facts` 只来自本轮新折叠的 `folded` rounds：`facts = _facts_from_rounds(folded)`。
4. 如果会话已经压缩过一次，后续继续探索并再次触发压缩，第一次摘要里的旧 `facts/tool_call_id` 不会被合并进第二次摘要。

影响：

- `session.evidence_index` 内存中还保留旧证据，但模型下一轮看不到旧 `tool_call_id`。
- 这破坏 `25-v3c-alignment.md` 的核心要求：压缩后 Finding 仍能引用压缩前的原始工具证据。
- 现有 `v3c_compare` 只覆盖一次压缩，无法发现这个问题。

修复要求：

1. 重复压缩时，解析最新一条旧 `is_compressed` 消息中的事实 JSON。
2. 新摘要应合并“旧摘要 facts + 本次 newly folded facts”，按 `tool_call_id` 去重，保持稳定顺序或排序。
3. `checked` / `excluded` / `pending` 继续从 session 集合生成，但 facts 不能只来自本次折叠窗口。
4. 增加测试：先压缩 `c0/c1`，再追加 `c2/c3` 并第二次压缩；断言最终唯一压缩摘要同时包含 `c0` 和新折叠的 id。
5. 增加 compare case：`compact-recompact-keeps-old-facts`。

建议实现方向：

- `_partition_rest()` 返回的 `compressed` 不要丢弃。
- 新增 `_facts_from_compressed(compressed)`，只解析最新一条压缩消息；解析失败则空列表，不崩溃。
- 合并时以 `tool_call_id` 为 key 去重，保留旧 facts，再追加新 facts。

## 4. 同轮建议修复（P2）

### P2-1：JSON repair pending 与 `compact_keep_rounds=0` 的边界未覆盖

位置：

- `reposage/review/agent/loop.py:178`
- `reposage/review/agent/loop.py:357`
- `reposage/review/agent/compact.py:81`

对齐稿允许 `compact_keep_rounds >= 0`，也明确 JSON repair pending 时仍可压缩。但当前压缩按轮折叠；当 `compact_keep_rounds=0` 且 parse error 后触发 compact，最后的“请修复 JSON” user 指令可能被折叠进摘要或直接丢失，修复请求下一轮就看不到真正的修复指令。

修复建议：

- 当 `session.json_repair_pending=True` 时，强制保留最后一条 repair user 消息，或把 repair 指令建模为不可折叠控制消息。
- 增加测试：`action_json` parse error 后制造超阈值上下文，`compact_keep_rounds=0`，断言第二次 provider 请求仍包含 repair instruction。

### P2-2：V3-C compare 覆盖不足

当前 `v3c_compare` 6/6 只覆盖一次压缩主路径。建议扩展到至少 8/8：

- `compact-recompact-keeps-old-facts`
- `compact-json-repair-keeps-repair-instruction`

这样能钉住本轮两个最容易回归的边界。

### P2-3：压缩日志未包含 `stubbed`

对齐稿要求日志至少包含 `before_chars`、`after_chars`、`folded_rounds`、`index_size`、`summary_ref`，当前已满足；但 `CompactResult` 已有 `stubbed` 字段，日志未输出。建议补上，便于后续观察“单条超长观察被 stub”的频率。此项非阻塞。

## 5. 已确认通过的点

- `maybe_compact` 没有调用 LLM，compact 不占 provider round。
- 初始 user 任务消息保留。
- native 尾窗口配对测试通过。
- `evidence_index` 已用于 `submit_finding`，不再只靠 `evidence_refs` 字符串。
- 工具成功写 `checked`，失败 / unknown / repeat / grace 拒绝写 `excluded`。
- WAITING_TOOL 中 compact no-op。
- 硬顶 overflow 仍存在。
- 默认 `agent.enabled=false`，默认 single_pass 不产生 tool_calls。
- SQLite `user_version` 仍为 5。

## 6. 复验门槛

下一轮请提供：

1. P1-1 代码修复与单元测试。
2. P2-1 测试与必要修复。
3. 更新 `v3c_compare` 到覆盖重复压缩与 JSON repair compact。
4. 全量 pytest、V3-C compare、V3-B compare、Ruff、Mypy 通过。
5. 复验前 `docs/evidence/v3-c-status.md` 继续保持 `NOT ACCEPTED`。

修完这两个边界后，V3-C 大概率可以进入验收。
