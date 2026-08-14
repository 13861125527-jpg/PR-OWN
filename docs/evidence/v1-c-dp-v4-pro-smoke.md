# V1-c DP-V4-PRO 真实 smoke 证据

> 状态：**待执行**（外部阻塞：本环境未配置 `MODEL_API_KEY` / `MODEL_BASE_URL`）
> **V1-c 里程碑状态：代码实现接近完成 + Mock 验证通过；真实模型验证未完成**
> （不得标记为最终通过，直至下方实测结果填写真实数据且成功率 ≥ 0.8）
> 生成方式：配置密钥后运行 `python -m reposage.providers.llm.smoke`（默认 20 轮），
> 并设 `OQ1_REPORT` 指向本目录以落盘脱敏 JSON。

## 执行步骤（安全）

```powershell
$env:MODEL_BASE_URL = "https://your-endpoint/v1"   # 不含任何凭证
$env:MODEL_API_KEY  = "sk-..."                      # 仅本地设置，禁止提交
$env:OQ1_ROUNDS     = "20"
$env:OQ1_REPORT     = "docs/evidence/v1-c-dp-v4-pro-smoke.json"
python -m reposage.providers.llm.smoke
```

验收标准：结构化解析成功率 ≥ 0.8（`14` OQ-1）。

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
