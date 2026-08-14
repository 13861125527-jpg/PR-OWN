# V1-c DP-V4-PRO 真实 smoke 证据

> 状态：**通过**（Round 3 复验，2026-08-14）
> 实际模型：`deepseek-v4-pro`（MODEL_NAME 覆盖，`MODEL_BASE_URL=https://api.deepseek.com`）
> **V1-c 里程碑状态：代码实现完成 + Mock 验证通过 + 真实模型验证通过（20/20）**
> 原始脱敏数据：`docs/evidence/v1-c-dp-v4-pro-smoke.json`

## 实测结果（Round 3）

| 项 | 结果 |
|---|---|
| 最小请求连通（延迟） | 通过（1880 ms） |
| 并发 3 请求（总延迟 / 429） | 通过（39393 ms / 0 次 429） |
| 结构化解析成功率（/20） | **20/20 = 100%**（0 结构失败、0 请求失败、0 修复） |
| 能力协商降级 | `schema_fallbacks = 20`（每轮 json_schema 不可用 → 降级 json_object 成功）；`response_format_fallbacks = 0` |
| HTTP 统计 | `http_attempts = 44`（minimal 1 + concurrency 3 + structured 20×2）、`http_retries = 0`、`http_429 = 0` |
| usage | input/output tokens 已记录，`cost_usd = 0`（未定价，V1-f 补价格） |
| 总体 | `passed = true` |
| 脱敏 | 通过（报告与日志无 API Key / Authorization） |

## OQ-1 能力结论

- OpenAI-compatible API 可用性：**已验证**（DeepSeek `/chat/completions`）
- 异步并发表现（429 频率）：**已验证**（并发 3 无 429）
- 稳定 JSON / 字段漂移率：**已验证**（20/20 解析成功、0 漂移失败；schema_first 降级 json_object 生效）
- 输出上限参数兼容性：**已验证**（`max_tokens` 被真实端点接受）
- 模型名可配置性：**已验证**（`MODEL_NAME` 覆盖，服务端契约 `deepseek-v4-pro`）
- tool calling（native）可用性：**留待 V3 方案 A/B 实测**（`08` §9 / OQ-11）
- 真实可用上下文长度：待实测（预算 32k 校准，`06` §2）
- 单价 / 成本：未定价（cost_usd=0，V1-f 成本监控补价格）

## 历史记录

- Round 1（`43fd85f` 前）：模型名 `DP-V4-PRO` 与 DeepSeek 契约不符 → 全部 400；修复 MODEL_NAME 覆盖
- Round 2（`bfb6820` 前）：`response_format=json_schema` 返回 `unavailable` 未识别 → 20 轮 400；修复 unavailable 识别 + 三级降级
- Round 3（`bfb6820` 后）：三级降级生效，20/20 通过

## 执行步骤（安全）

模型名覆盖：`MODEL_NAME` 环境变量 > 配置 `llm.model` > 内置默认值（不硬编码
具体服务商模型名；真实服务端契约要求如 DeepSeek 用 `deepseek-v4-pro`）。

**PowerShell：**

```powershell
$env:MODEL_BASE_URL = "https://api.deepseek.com"        # 不含任何凭证
$env:MODEL_API_KEY  = "sk-..."                           # 仅本地设置，禁止提交
$env:MODEL_NAME     = "deepseek-v4-pro"                  # 覆盖默认 DP-V4-PRO
$env:OQ1_ROUNDS     = "20"
$env:OQ1_REPORT     = "docs/evidence/v1-c-dp-v4-pro-smoke.json"
python -m reposage.providers.llm.smoke
Remove-Item Env:MODEL_API_KEY                            # 测试后清除 Key
```

**CMD：**

```bat
set "MODEL_BASE_URL=https://api.deepseek.com"
set "MODEL_API_KEY=sk-..."
set "MODEL_NAME=deepseek-v4-pro"
set "OQ1_ROUNDS=20"
set "OQ1_REPORT=docs/evidence/v1-c-dp-v4-pro-smoke.json"
python -m reposage.providers.llm.smoke
set "MODEL_API_KEY="
```

smoke 启动时打印实际模型名（不打印 API Key）；验收标准：结构化解析成功率
≥ 0.8 且 minimal/concurrency/structured 三项强制检查全通过（`14` OQ-1）。

## 脱敏规则

- 报告 JSON 不包含 api key、`Authorization` header、完整请求源码；
- `base_url` 已剥离查询参数与 userinfo 凭证片段；
- 失败 detail 截断至 160 字符；
- 敏感模型响应不得复制进本文件。

## 实测结果（执行后填写）

| 项 | 结果 |
|---|---|
| 最小请求连通（延迟） | 待执行 |
| 并发 3 请求（总延迟 / 429） | 待执行 |
| 结构化解析成功率（/20） | 待执行 |
| 字段漂移/结构失败次数 | 待执行 |
| 修复重试 / HTTP 重试 / 429 次数 | 待执行 |
| usage（input/output tokens，cost 未定价标 0） | 待执行 |
| 真实可用上下文长度 | 待实测（预算 32k 校准，`06` §2） |
| 输出上限参数兼容性（`max_tokens` vs 兼容字段） | 待确认（P1-3） |

## OQ-1 能力结论（执行后填写）

- OpenAI-compatible API 可用性：
- 异步并发表现（429 频率）：
- 稳定 JSON / 字段漂移率：
- tool calling（native）可用性：V3 方案 A/B 前实测（`08` §9）
- 单价 / 成本：未定价（cost_usd=0，V1-f 成本监控补价格）
