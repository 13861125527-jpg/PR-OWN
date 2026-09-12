# V3-A 实现验收（Round 1）

> 结论：**CHANGES REQUIRED / 暂不通过**  
> 日期：2026-08-16  
> 依据：`docs/architecture/23-v3a-alignment.md`

## 1. 总体结论

V3-A 主体已经正确落地：Registry、Pydantic Schema、统一 `ToolSnapshot`、锁定 SHA 读取、路径沙箱、字面量 `search_code`、控制工具 stub、取消重抛、原子工具审计和 agentic 失败快均已实现。现有测试、类型、规范和 V2 回归全部通过。

但仍有 2 个 P1 安全边界未闭合，且现有测试没有覆盖。修复后再复验即可，不需要重构整体工具层。

## 2. 阻塞问题

### P1-1：`find_references` 对源码扫描无字符硬上限，工具超时可能失效

文件：`reposage/tools/find_references.py`

当前只限制 `MAX_FILES=100`，但每个 blob 可以无限大。随后在事件循环线程内同步执行：

- Python AST 符号抽取；
- `blob.splitlines()`；
- 对每行执行字符串和单词扫描。

`asyncio.wait_for` 只能在协程重新让出控制权时触发。若一个超大文件正在同步 AST/逐行处理，15 秒超时不能按时打断，和 V3-A 的“有界只读工具”契约不一致。

一次性修改要求：

- 增加 `MAX_CHARS_PER_FILE` 与 `MAX_TOTAL_CHARS_SCANNED`；
- 在进入 `SymbolIndex.defs_for()` 和逐行扫描前裁剪；
- 达到文件数、单文件字符或总字符上限时设置 `truncated=true`；
- 路径继续稳定排序；
- 增加超大单文件与总扫描量测试，验证处理输入确实不超过上限。

建议与 `search_code` 使用同一量级：单文件 4k～16k、总量 200k；具体数值可以按符号抽取效果调整，但必须是固定硬上限。

### P1-2：审计层没有限制 `args_json/data` 大小，非法参数可造成数据库膨胀

文件：`reposage/tools/invoke.py`、`reposage/storage/sqlite.py`

Schema 校验失败时，`ToolCall.arguments` 仍保留完整 `raw_args`，随后 `_finish()` 会写入审计。攻击性或异常模型可以传入超长字段；即使 Schema 正确返回 `invalid_args`，SQLite 仍会保存完整参数。

同时 `record_tool_invocation()` 是公开 Storage 边界，当前对 `result.data` 只脱敏、不限制长度；绕过 `invoke()` 直接调用时也可写入超大数据。这破坏了对齐稿中“trace 全字段脱敏且有大小限制”的要求。

一次性修改要求：

- 为审计参数设置固定上限，例如序列化后最多 8k chars；
- 保持 JSON 可解析：按字段安全裁剪或存结构化摘要，禁止从 JSON 字符串中间截断；
- Storage 边界再次强制 `args_json` 与 `data` 上限，不能只信任 invoke；
- 超限信息明确标记 `truncated` 或写稳定摘要/hash；
- 幂等比较使用“脱敏并限长后的规范化值”；
- 添加 invalid_args 超长参数、直接 Storage 超长 data、同值幂等和冲突测试。

## 3. 非阻塞改进

### P2-1：通用信封的最终硬上限在极小配置下不一定成立

`encode_tool_data()` 最后的 `{"truncated": true}` 信封本身可能大于调用方配置的 `max_chars`。内置工具的上限足够大，因此当前产品路径不受影响。建议给 `ToolDefinition.max_result_chars` 增加合理最小值校验，或在无法容纳最小信封时返回稳定 error，避免未来注册自定义工具时出现契约例外。

### P2-2：取消测试只验证重抛，未验证审计状态

当前取消传播正确，但测试没有覆盖“开启 trace + 有 task_id”时最终只记录 `cancelled`，且审计失败不阻止取消传播。建议和本轮测试一起补齐。

## 4. 已通过项目

- 五个只读工具只依赖统一异步 `ToolSnapshot`。
- Git adapter 使用锁定 SHA，并先校验快照路径集合。
- 绝对路径、目录穿越、盘符、UNC、symlink escape 被拒绝。
- Schema 使用 `extra="forbid"`，未知工具与非法参数进入统一错误协议。
- `search_code` 是字面量搜索，具有文件数、单文件字符数、总字符数和命中数上限。
- ToolResult 使用完整 JSON 信封，source SHA 可追溯。
- `submit_finding` / `finish_review` 只返回 `not_bound`，不进入 Pipeline。
- `strategy=agentic` 在 V3-B 前失败快；默认审查不会产生 tool call。
- tool call/result 使用同一事务；同值幂等、冲突拒绝、task FK 校验和 secret redaction 已实现。
- 取消会重新抛出 `CancelledError`。

## 5. 门禁结果

| 门禁 | 结果 |
|---|---|
| V3-A/Storage/Service 针对性测试 | PASS（1 个 symlink 环境性 skip） |
| 全量 pytest | PASS（1 个 symlink 环境性 skip） |
| Ruff | PASS |
| MyPy strict | PASS（90 source files） |
| V3-A compare | PASS（10/10） |
| V2-A～V2-E 回归 compare | 全部 PASS |

现有绿灯证明正常路径与既有对抗集正确，但不覆盖上述两个超大输入边界，因此不能替代修复。

## 6. 最终判定

**V3-A Round 1：暂不通过。**

请一次性修复 P1-1、P1-2，并顺手补 P2 测试；下一轮只需针对这几个点和全量门禁复验。
