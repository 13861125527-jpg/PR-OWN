# V3-A 实现验收（Round 2）

> 结论：**ACCEPTED / 通过**  
> 日期：2026-08-16  
> 前轮：`docs/evidence/v3-a-implementation-review-round1.md`

## 1. 验收结论

V3-A Round 1 的两个阻塞问题与两个非阻塞改进均已完成，并具有针对性测试。未发现新的功能、安全或兼容性阻塞项。V3-A 可以正式验收并进入 V3-B 设计阶段。

## 2. Round 1 问题复验

### P1-1：引用搜索扫描无字符硬上限 —— 已修复

- `find_references` 增加 `MAX_CHARS_PER_FILE=16_000`。
- 增加 `MAX_TOTAL_CHARS_SCANNED=200_000`。
- 在 AST 抽取与逐行扫描之前裁剪源码。
- 文件路径稳定排序。
- 单文件、总字符或文件数达到上限时设置 `truncated=true`。
- 新增超大单文件与多文件总扫描量测试。

结论：同步 AST/逐行工作的输入规模已固定有界，上轮超大文件导致超时失效的风险已关闭。

### P1-2：审计参数与结果无大小限制 —— 已修复

- 新增统一工具审计规范化模块。
- `args_json` 上限 8,000 chars；`data` 上限 32,000 chars；error 上限 256 chars。
- 超限内容保存为可解析的结构化摘要，包含原长度与 SHA-256，不做字符串中间截断。
- Storage 边界强制再次规范化，不依赖 invoke 的调用纪律。
- 幂等与冲突比较基于脱敏、限长后的规范化值。
- 审计发生裁剪时数据库 `truncated=1`。
- 新增 invalid_args 超长参数、Storage 直接写入超长 data、幂等与冲突测试。

结论：模型即使提交异常超长参数，也不能无限放大 SQLite trace。

### P2-1：极小结果上限 —— 已修复

- `ToolDefinition.max_result_chars` 增加最小值 512。
- 最小结构化信封仍无法容纳时返回稳定 `result_too_large`，不违反硬上限。

### P2-2：取消审计测试 —— 已补齐

- 开启 trace 且存在 task 时，取消落库状态为 `cancelled`。
- 审计存储故障时仍重新抛出 `CancelledError`。
- 未把取消误记为 `ok/timeout`。

## 3. 最终能力确认

- Registry、Schema、`extra="forbid"`、未知工具错误协议成立。
- 统一异步 `ToolSnapshot` 与锁定 head SHA 读取成立。
- 路径穿越、绝对路径、盘符、UNC 与 symlink escape 防护成立。
- `read_file/find_files/search_code/find_references/read_diff` 均为有界只读工具。
- `search_code` 只执行字面量搜索，不执行用户正则。
- ToolResult 保持完整 JSON 信封与 source SHA 追溯。
- `submit_finding/finish_review` 仍为 `not_bound` stub，不进入 Pipeline。
- tool call/result 原子落库、幂等、冲突拒绝、脱敏与大小限制成立。
- `strategy=agentic` 在 V3-B 前失败快，默认 V2 审查路径不产生工具调用。
- 未接入 Agent loop、`tool_loop`、AgenticReviewer、MCP 或写工具。

## 4. 门禁结果

| 门禁 | 结果 |
|---|---|
| V3-A/Storage 针对性测试 | PASS（1 个 symlink 环境性 skip） |
| 全量 pytest | PASS（1 个 symlink 环境性 skip） |
| Ruff | PASS |
| MyPy strict | PASS（91 source files） |
| V3-A compare | PASS（10/10） |
| V2-A～V2-E 回归 compare | 全部 PASS |

symlink skip 仅因当前 Windows 环境缺少创建符号链接权限；字符串级路径对抗与 resolve 测试均执行通过，不构成功能失败。

## 5. 最终判定

**V3-A Round 2：验收通过。**

下一步进入 V3-B（Agent Loop）设计对齐。V3-B 开工前应按既定架构完成真实模型 tool calling 协议实测与 native/text 两种方案裁决；该事项不回溯阻塞 V3-A。
